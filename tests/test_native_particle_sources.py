"""SYSTEM_VERIFY_ONLY: real USD/PhysxSchema source rejection, without activating physics."""
import sys
from types import ModuleType,SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import numpy as np


class NativeParticleSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from runtime import simulation
        try:
            simulation._initialize_usd_schemas()
            from pxr import Gf,PhysxSchema,Sdf,Usd,UsdGeom,UsdPhysics
        except (ImportError,RuntimeError) as exc:
            raise unittest.SkipTest('requires the already installed locked SDK CPU schema ABI: '+str(exc))
        cls.simulation=simulation
        cls.Gf,cls.PhysxSchema,cls.Sdf,cls.Usd,cls.UsdGeom,cls.UsdPhysics=Gf,PhysxSchema,Sdf,Usd,UsdGeom,UsdPhysics

    def fixture(self):
        stage=self.Usd.Stage.CreateInMemory()
        scene=self.UsdPhysics.Scene.Define(stage,'/World/physicsScene')
        self.PhysxSchema.PhysxSceneAPI.Apply(scene.GetPrim()).CreateEnableGPUDynamicsAttr(True)
        system=self.PhysxSchema.PhysxParticleSystem.Define(stage,'/World/system')
        system.CreateParticleSystemEnabledAttr(True)
        system.CreateSimulationOwnerRel().SetTargets([scene.GetPath()])
        points=self.UsdGeom.Points.Define(stage,'/World/particles')
        points.CreatePointsAttr([self.Gf.Vec3f(0,0,1)])
        points.CreateVelocitiesAttr([self.Gf.Vec3f(0)])
        self.PhysxSchema.PhysxParticleSetAPI.Apply(points.GetPrim()).CreateFluidAttr(True)
        api=self.PhysxSchema.PhysxParticleAPI(points.GetPrim())
        api.CreateParticleEnabledAttr(True);api.CreateParticleSystemRel().SetTargets([system.GetPath()])
        mass=self.UsdPhysics.MassAPI.Apply(points.GetPrim());mass.CreateMassAttr(.001);mass.CreateDensityAttr(0.)
        io={'streams':{'state':{'source':{'reader':'particle_state','path':'/World/particles','field':'positions'}}}}
        return stage,scene,system,points,io

    def test_only_source_binding_is_admitted_without_claiming_an_active_particle_actor(self):
        stage,_,_,_,io=self.fixture()
        self.assertEqual(self.simulation._prepare_native_particle_sources(stage,io),{'/World/particles'})

    def test_authored_output_disabled_set_or_system_invalid_owner_and_cpu_scene_fail(self):
        for failure in ('authored_output','disabled_set','disabled_system','missing_owner','wrong_owner','cpu','zero_mass','empty_input','transformed_frame'):
            with self.subTest(failure=failure):
                stage,scene,system,points,io=self.fixture()
                if failure=='authored_output':
                    self.PhysxSchema.PhysxParticleSetAPI(points.GetPrim()).CreateSimulationPointsAttr([self.Gf.Vec3f(1)])
                elif failure=='disabled_set':self.PhysxSchema.PhysxParticleAPI(points.GetPrim()).GetParticleEnabledAttr().Set(False)
                elif failure=='disabled_system':system.GetParticleSystemEnabledAttr().Set(False)
                elif failure=='missing_owner':system.GetSimulationOwnerRel().SetTargets([])
                elif failure=='wrong_owner':system.GetSimulationOwnerRel().SetTargets(['/World/missing'])
                elif failure=='cpu':self.PhysxSchema.PhysxSceneAPI(scene.GetPrim()).GetEnableGPUDynamicsAttr().Set(False)
                elif failure=='zero_mass':self.UsdPhysics.MassAPI(points.GetPrim()).GetMassAttr().Set(0.)
                elif failure=='empty_input':points.GetPointsAttr().Set([]);points.GetVelocitiesAttr().Set([])
                else:points.AddTranslateOp().Set(self.Gf.Vec3d(1,0,0))
                with self.assertRaises(ValueError):self.simulation._prepare_native_particle_sources(stage,io)

    def readback_fixture(self):
        stage,scene,system,points,io=self.fixture()
        env=object.__new__(self.simulation.Environment)
        env.stage=stage;env._qualified_particle_paths=self.simulation._prepare_native_particle_sources(stage,io)
        env._particle_cache={}
        actual={'stageId':9,'absolutePhysicsStep':2,'simulationTime':.01}
        env._clock=lambda:{'time':actual['simulationTime']-.01,'simulationTime':actual['simulationTime'],
            'timeOrigin':.01,'physicsStep':actual['absolutePhysicsStep']-2,'stageId':actual['stageId']}
        flags={'/physics/suppressReadback':False,'/physics/updateToUsd':True,
            '/physics/updateParticlesToUsd':True,'/physics/updateVelocitiesToUsd':True}
        names=('carb','omni','omni.physx','omni.physx.bindings','omni.physx.bindings._physx',
            'isaacsim','isaacsim.core','isaacsim.core.simulation_manager')
        modules={name:ModuleType(name) for name in names}
        for name,module in modules.items():
            parent,_,child=name.rpartition('.')
            if parent in modules:setattr(modules[parent],child,module)
        modules['carb'].settings=SimpleNamespace(get_settings=lambda:SimpleNamespace(get_as_bool=lambda key:bool(flags.get(key))))
        bindings=modules['omni.physx.bindings._physx']
        bindings.SETTING_UPDATE_TO_USD='/physics/updateToUsd'
        bindings.SETTING_UPDATE_PARTICLES_TO_USD='/physics/updateParticlesToUsd'
        bindings.SETTING_UPDATE_VELOCITIES_TO_USD='/physics/updateVelocitiesToUsd'
        fetch=Mock();update=Mock()
        modules['omni.physx'].get_physx_simulation_interface=lambda:SimpleNamespace(fetch_results=fetch)
        modules['omni.physx'].get_physx_interface=lambda:SimpleNamespace(get_overwrite_gpu_setting=lambda:-1,update_transformations=update)
        manager=SimpleNamespace(get_physics_scenes=lambda:[SimpleNamespace(prim=scene.GetPrim(),get_enabled_gpu_dynamics=lambda:True)],
            get_active_physics_engine=lambda:'physx',is_simulating=lambda:True)
        modules['isaacsim.core.simulation_manager'].SimulationManager=manager
        self.enterContext(patch.dict(sys.modules,modules))
        self.enterContext(patch.object(self.simulation,'_physical_clock',side_effect=lambda _:dict(actual)))
        return env,system,points,flags,actual,fetch,manager

    def read(self,env,field='positions'):
        return env._read_particles({'reader':'particle_state','path':'/World/particles','field':field})

    def test_unprocessed_positions_are_native_without_simulation_points_or_velocity_readback(self):
        env,_,points,flags,_,_,_=self.readback_fixture()
        flags['/physics/updateVelocitiesToUsd']=False
        flags['/physics/updateParticlesToUsd']=False
        points.GetVelocitiesAttr().Clear()
        np.testing.assert_array_equal(self.read(env),[[0,0,1]])

    def test_active_fluid_smoothing_reads_raw_buffer_and_never_substitutes_render_points(self):
        env,system,points,_,_,_,_=self.readback_fixture()
        self.PhysxSchema.PhysxParticleSmoothingAPI.Apply(system.GetPrim())
        with self.assertRaises(ValueError):self.read(env)
        self.PhysxSchema.PhysxParticleSetAPI(points.GetPrim()).CreateSimulationPointsAttr([self.Gf.Vec3f(0,0,2)])
        np.testing.assert_array_equal(self.read(env),[[0,0,2]])

    def test_disabled_smoothing_and_solid_sets_ignore_residual_simulation_points(self):
        for fluid,enabled in ((True,False),(False,True)):
            with self.subTest(fluid=fluid,enabled=enabled):
                env,system,points,_,_,_,_=self.readback_fixture()
                self.PhysxSchema.PhysxParticleSetAPI(points.GetPrim()).CreateSimulationPointsAttr([self.Gf.Vec3f(9)])
                self.PhysxSchema.PhysxParticleSetAPI(points.GetPrim()).GetFluidAttr().Set(fluid)
                self.PhysxSchema.PhysxParticleSmoothingAPI.Apply(system.GetPrim()).CreateParticleSmoothingEnabledAttr(enabled)
                np.testing.assert_array_equal(self.read(env),[[0,0,1]])

    def test_velocity_reader_does_not_require_position_output_or_smoothing_buffer(self):
        env,system,points,_,_,_,_=self.readback_fixture()
        self.PhysxSchema.PhysxParticleSmoothingAPI.Apply(system.GetPrim())
        points.GetPointsAttr().Clear()
        np.testing.assert_array_equal(self.read(env,'velocities'),[[0,0,0]])

    def test_smoothing_velocity_updates_without_particle_position_readback(self):
        env,system,points,flags,_,_,_=self.readback_fixture()
        self.PhysxSchema.PhysxParticleSmoothingAPI.Apply(system.GetPrim())
        flags['/physics/updateParticlesToUsd']=False
        points.GetVelocitiesAttr().Set([self.Gf.Vec3f(0,0,-2)])
        np.testing.assert_array_equal(self.read(env,'velocities'),[[0,0,-2]])
        with self.assertRaises(ValueError):self.read(env)

    def test_readback_flags_are_checked_per_field_even_after_cache_hit(self):
        env,_,_,flags,_,_,_=self.readback_fixture()
        np.testing.assert_array_equal(self.read(env,'velocities'),[[0,0,0]])
        flags['/physics/updateVelocitiesToUsd']=False
        with self.assertRaises(ValueError):self.read(env,'velocities')
        np.testing.assert_array_equal(self.read(env),[[0,0,1]])
        for key,value in (('/physics/suppressReadback',True),('/physics/updateToUsd',False)):
            with self.subTest(key=key):
                old=flags[key];flags[key]=value
                with self.assertRaises(ValueError):self.read(env)
                flags[key]=old

    def test_isosurface_reads_points_but_requires_particle_readback(self):
        env,system,_,flags,_,_,_=self.readback_fixture()
        self.PhysxSchema.PhysxParticleIsosurfaceAPI.Apply(system.GetPrim())
        np.testing.assert_array_equal(self.read(env),[[0,0,1]])
        flags['/physics/updateParticlesToUsd']=False
        with self.assertRaises(ValueError):self.read(env)

    def test_source_fields_are_not_native_before_actual_physics_or_when_stopped(self):
        for step,time,running in ((0,0.,True),(2,.01,False)):
            with self.subTest(step=step,running=running):
                env,_,_,_,actual,_,manager=self.readback_fixture()
                actual.update(absolutePhysicsStep=step,simulationTime=time)
                manager.is_simulating=lambda:running
                with self.assertRaises(ValueError):self.read(env)

    def test_requested_field_rejects_missing_or_nonfinite_buffers(self):
        for field,invalid in (('positions',None),('velocities',None),('velocities',[self.Gf.Vec3f(float('nan'))])):
            with self.subTest(field=field,invalid=invalid):
                env,_,points,_,_,_,_=self.readback_fixture()
                attr=points.GetPointsAttr() if field=='positions' else points.GetVelocitiesAttr()
                if invalid is None:attr.Clear()
                else:attr.Set(invalid)
                with self.assertRaises(ValueError):self.read(env,field)

    def test_zero_particle_readback_keeps_actual_native_shape(self):
        env,_,points,_,_,_,_=self.readback_fixture()
        points.GetPointsAttr().Set([])
        self.assertEqual(self.read(env).shape,(0,3))

    def test_same_clock_readback_setting_changes_replace_cached_values(self):
        env,_,points,flags,_,fetch,_=self.readback_fixture()
        np.testing.assert_array_equal(self.read(env),[[0,0,1]])
        flags['/physics/updateVelocitiesToUsd']=False
        points.GetPointsAttr().Set([self.Gf.Vec3f(0,0,2)])
        np.testing.assert_array_equal(self.read(env),[[0,0,2]])
        flags['/physics/updateVelocitiesToUsd']=True
        points.GetPointsAttr().Set([self.Gf.Vec3f(0,0,3)])
        np.testing.assert_array_equal(self.read(env),[[0,0,3]])
        self.assertEqual(fetch.call_count,3)

    def test_same_clock_position_and_velocity_shapes_must_agree_when_both_are_read(self):
        env,_,points,_,_,_,_=self.readback_fixture()
        self.read(env)
        points.GetVelocitiesAttr().Set([self.Gf.Vec3f(0)]*2)
        with self.assertRaisesRegex(ValueError,'different shapes'):self.read(env,'velocities')

    def test_synchronization_cannot_advance_clock(self):
        env,_,_,_,actual,fetch,_=self.readback_fixture()
        fetch.side_effect=lambda:actual.update(absolutePhysicsStep=3,simulationTime=.015)
        with self.assertRaisesRegex(ValueError,'clock'):self.read(env)

    def test_same_clock_mode_changes_replace_cache_instead_of_reviving_earlier_values(self):
        env,system,points,_,_,fetch,_=self.readback_fixture()
        np.testing.assert_array_equal(self.read(env),[[0,0,1]])
        smooth=self.PhysxSchema.PhysxParticleSmoothingAPI.Apply(system.GetPrim())
        raw=self.PhysxSchema.PhysxParticleSetAPI(points.GetPrim()).CreateSimulationPointsAttr([self.Gf.Vec3f(0,0,2)])
        np.testing.assert_array_equal(self.read(env),[[0,0,2]])
        smooth.CreateParticleSmoothingEnabledAttr(False)
        points.GetPointsAttr().Set([self.Gf.Vec3f(0,0,3)])
        np.testing.assert_array_equal(self.read(env),[[0,0,3]])
        smooth.GetParticleSmoothingEnabledAttr().Set(True);raw.Set([self.Gf.Vec3f(0,0,4)])
        np.testing.assert_array_equal(self.read(env),[[0,0,4]])
        self.assertEqual(fetch.call_count,4)


if __name__=='__main__':unittest.main()

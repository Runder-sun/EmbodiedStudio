"""SYSTEM_VERIFICATION_ONLY: CPU I/O and transaction fixtures, never SDK evidence."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from runtime import records


IO = {'schema':'sceneagent-native-io-v1', 'clock':{'physicsRateHz':500,'controlRateHz':50},
    'ports':{'arm':{'source':{'primitive':'joint_position','path':'/World/test_robot','joints':['a','b','c']},
        'dtype':'float64','shape':[3],'units':['rad']*3,'limits':[[-2,2]]*3,'persistence':'persistent'}},
    'streams':{
        'q':{'source':{'reader':'joint_positions','path':'/World/test_robot','joints':['a','b','c']},
             'dtype':'float64','shape':[3],'units':['rad']*3,'frame':'joint','origin':'native_output',
             'rateHz':50,'visibleToPolicy':True,'required':True},
        'nodes':{'source':{'reader':'native_points','path':'/World/test_points'},
             'dtype':'float32','shape':[None,3],'units':'m','frame':'world','origin':'native_output',
             'rateHz':50,'visibleToPolicy':False,'required':True}}}
COMMAND = {'arm':[.1,.2,.3]}


class Scorer:
    descriptor = {'sourceRef':'rubrics.py','sourceDigest':'a'*64,'helperDigests':{}}
    def __init__(self):self.state=None;self.calls=[]
    def preview(self, phase, facts):
        self.calls.append((phase,deepcopy(facts)))
        return {'state':{'cycle':0 if phase=='reset' else (self.state or {})['cycle']+1},
                'result':{'success':False,'terminated':False,'reward':None,'details':{}}}
    def accept(self, staged):self.state=staged['state']


def observation(t, *, truth=False):
    value={'time':t,'simulationTime':1+t,'timeOrigin':1.,'physicsStep':round(t*500),'stageId':'cpu-stage',
           'streams':{'q':[0.,0.,0.]},'streamStatus':{},'cameraFrames':{}}
    if truth:value['streams']['nodes']=np.arange(210,dtype=np.float32).reshape(70,3)
    return value


def begin(directory, io=None):
    recorder=records.Recorder(directory,io=deepcopy(io or IO)); scorer=Scorer()
    recorder.begin({'observation':observation(0),'truth':observation(0,truth=True)},scorer)
    return recorder,scorer


def sampled_cycle(control_rate, start=0):
    io=deepcopy(IO);io['clock']={'physicsRateHz':200,'controlRateHz':control_rate}
    io['streams']['q']['rateHz']=100;io['streams']['nodes']['rateHz']=control_rate
    def state(step,truth=False):
        value=observation(step/200,truth=truth);value['physicsStep']=step
        sampled=step-step%2
        value['streams']['q']=[sampled/200]*3
        value['streamStatus']['q']={'status':'available','sampleTime':sampled/200,'physicsStep':sampled}
        return value
    end=start+200//control_rate
    samples=[]
    for step in range(start+1,end+1):
        sample=state(step)
        if step%2:sample['streams']={};sample['streamStatus']={}
        samples.append(sample)
    return io,state(start),state(start,True),state(end),state(end,True),samples


class NativeRecordsTests(unittest.TestCase):
    def test_large_public_arrays_reuse_committed_content_without_reducing_commit_durability(self):
        io=deepcopy(IO);io['streams']['nodes']['visibleToPolicy']=True
        def state(t):return observation(t,truth=True)
        with tempfile.TemporaryDirectory() as directory:
            recorder=records.Recorder(directory,io=io)
            recorder.begin({'observation':state(0),'truth':state(0)},Scorer())
            with patch.object(np,'save',wraps=np.save) as saved,patch.object(records.os,'fsync',wraps=records.os.fsync) as synced:
                recorder.commit(state(0),COMMAND,COMMAND,state(.02),10,truth_before=state(0),truth_after=state(.02))
            self.assertEqual(saved.call_count,0)
            self.assertEqual(synced.call_count,2)  # Reset content is immutable; both transaction barriers remain.
            recorder.close('completed')
            rows,_,_=records.read_transitions(directory)
            references=[rows[0][key]['streams']['nodes'] for key in ('observation','nextObservation','truthBefore','truthAfter')]
            self.assertTrue(all(reference==references[0] for reference in references))

    def test_array_validation_cache_is_local_and_rechecks_changed_file_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            reference=records._array_reference(directory,np.arange(210,dtype=np.float32).reshape(70,3))
            cache={}
            with patch.object(records,'_file_digest',wraps=records._file_digest) as digested:
                records.read_array(directory,reference,_cache=cache)
                records.read_array(directory,reference,_cache=cache)
                self.assertEqual(digested.call_count,1)
                records.read_array(directory,reference,_cache={})
                self.assertEqual(digested.call_count,2)
            path=Path(directory)/reference['ref'];raw=bytearray(path.read_bytes());raw[-1]^=1;path.write_bytes(raw)
            with self.assertRaisesRegex(ValueError,'digest'):
                records.read_array(directory,reference,_cache=cache)

    def test_record_prefix_reuses_unchanged_arrays_within_one_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder,_=begin(directory)
            for step in (0,10):
                recorder.commit(observation(step/500),COMMAND,COMMAND,observation((step+10)/500),10,
                    truth_before=observation(step/500,truth=True),truth_after=observation((step+10)/500,truth=True))
            recorder.close('completed')
            with patch.object(records,'_file_digest',wraps=records._file_digest) as digested:
                records.read_transitions(directory)
                self.assertEqual(digested.call_count,1)  # Every use still checks the immutable file identity.

    def test_named_commands_and_typed_arrays_share_one_committed_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder,scorer=begin(directory)
            recorder.commit(observation(0),COMMAND,COMMAND,observation(.02),10,
                truth_before=observation(0,truth=True),truth_after=observation(.02,truth=True))
            recorder.close('completed')
            rows,index,tail=records.read_transitions(directory)
            self.assertEqual(index['schema'],'sceneagent-control-transitions-v6')
            self.assertEqual(index['io'],IO);self.assertFalse(tail)
            self.assertEqual(rows[0]['executedAction'],COMMAND)
            self.assertNotIn('noise',rows[0]);self.assertNotIn('actionSpec',index)
            reference=rows[0]['truthAfter']['streams']['nodes']
            self.assertEqual(reference['dtype'],'float32');self.assertEqual(reference['shape'],[70,3])
            np.testing.assert_array_equal(records.read_array(directory,reference),np.arange(210,dtype=np.float32).reshape(70,3))
            self.assertNotIn('nodes',recorder.committed_observation['streams'])
            self.assertEqual(len(scorer.calls),2)

    def test_commands_reject_unknown_ports_wrong_dimensions_and_outside_limits(self):
        for command in ({'other':[0.]},{'arm':[0.,0.]},{'arm':[0.,0.,3.]},{'arm':[True,0.,0.]}):
            with tempfile.TemporaryDirectory() as directory,self.subTest(command=command):
                recorder,_=begin(directory)
                with self.assertRaises(ValueError):recorder.commit(observation(0),command,command,observation(.02),10)
                self.assertEqual(recorder.cycles,0);recorder.close('interrupted')

    def test_origin_source_and_policy_stream_set_are_frozen(self):
        for kind in ('hidden','unknown','source','claimed'):
            with tempfile.TemporaryDirectory() as directory,self.subTest(kind=kind):
                recorder,_=begin(directory);after=observation(.02)
                if kind=='hidden':after['streams']['nodes']=np.zeros((2,3),dtype=np.float32)
                elif kind=='unknown':after['streams']['claimed_mass']=123.
                elif kind=='source':after['streamStatus']['q']={'status':'available','origin':'native_output','source':{'reader':'claimed'}}
                else:after['claimed_mass']=123.
                with self.assertRaises(ValueError):recorder.commit(observation(0),COMMAND,COMMAND,after,10)
                recorder.close('interrupted')

    def test_optional_unavailable_is_explicit_and_never_zero_filled(self):
        io=deepcopy(IO);io['streams']['q']['required']=False
        with tempfile.TemporaryDirectory() as directory:
            recorder,_=begin(directory,io);after=observation(.02);after['streams']={}
            after['streamStatus']={'q':{'status':'unavailable','reason':'CPU fixture missing source'}}
            recorder.commit(observation(0),COMMAND,COMMAND,after,10,truth_before=observation(0,truth=True),
                truth_after={**observation(.02,truth=True),'streams':{'nodes':np.zeros((2,3),dtype=np.float32)},
                    'streamStatus':deepcopy(after['streamStatus'])})
            recorder.close('completed');batch=records.load_batch(directory,io=io)
            self.assertNotIn('q',batch['structuredNextObservations'][0]['streams'])
            self.assertIn('unavailable',json.dumps(batch['structuredNextObservations'][0]))

    def test_async_sample_cannot_be_future_or_change_physical_clock(self):
        for kind in ('future','clock','stage'):
            with tempfile.TemporaryDirectory() as directory,self.subTest(kind=kind):
                recorder,_=begin(directory);after=observation(.02)
                if kind=='future':after['streamStatus']['q']={'status':'available','sampleTime':.04}
                elif kind=='clock':after['simulationTime']+=.001
                else:after['stageId']='other'
                with self.assertRaises(ValueError):recorder.commit(observation(0),COMMAND,COMMAND,after,10)
                recorder.close('interrupted')

    def test_array_digest_dtype_shape_and_scope_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder,_=begin(directory)
            reference=recorder.index['initialSnapshot']['truth']['streams']['nodes']
            for change in ({'sha256':'b'*64},{'dtype':'float64'},{'shape':[35,6]},{'ref':'../outside.npy'}):
                with self.subTest(change=change),self.assertRaises(ValueError):
                    records.read_array(directory,{**reference,**change})
            recorder.close('interrupted')

    def test_object_dtype_is_never_serialized_or_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder=records.Recorder(directory,io=deepcopy(IO))
            initial=observation(0,truth=True);initial['streams']['nodes']=np.asarray([object()],dtype=object)
            with self.assertRaises(ValueError):recorder.begin({'observation':observation(0),'truth':initial},Scorer())
            recorder.close('interrupted')

    def test_failed_index_keeps_array_and_row_outside_committed_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder,scorer=begin(directory);before=deepcopy(scorer.state)
            with patch('runtime.records.atomic_json',side_effect=OSError('CPU index failure')):
                with self.assertRaises(OSError):
                    recorder.commit(observation(0),COMMAND,COMMAND,observation(.02),10,
                        truth_before=observation(0,truth=True),truth_after=observation(.02,truth=True))
            self.assertEqual(scorer.state,before)
            self.assertEqual(recorder.committed_observation,observation(0))
            recorder.close('interrupted');rows,index,tail=records.read_transitions(directory)
            self.assertEqual(rows,[]);self.assertTrue(tail);self.assertEqual(index['physicalSteps'],0)

    def test_failed_bulk_fsync_never_allows_later_commit_or_policy_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder,scorer=begin(directory);after=observation(.02,truth=True)
            after['streams']['nodes']+=1
            with patch('runtime.records.os.fsync',side_effect=OSError('CPU array fsync failure')):
                with self.assertRaises(OSError):
                    recorder.commit(observation(0),COMMAND,COMMAND,observation(.02),10,
                        truth_before=observation(0,truth=True),truth_after=after)
            self.assertEqual(recorder.committed_observation,observation(0))
            self.assertEqual(len(scorer.calls),1)
            with self.assertRaisesRegex(RuntimeError,'failed'):
                recorder.commit(observation(0),COMMAND,COMMAND,observation(.02),10)
            recorder.close('interrupted')

    def test_consumer_requires_the_frozen_io_and_preserves_structured_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder,_=begin(directory)
            recorder.commit(observation(0),COMMAND,COMMAND,observation(.02),10,
                truth_before=observation(0,truth=True),truth_after=observation(.02,truth=True))
            recorder.close('completed');changed=deepcopy(IO);changed['ports']['arm']['units']=['m']*3
            with self.assertRaisesRegex(ValueError,'frozen|contract'):records.load_batch(directory,io=changed)
            batch=records.load_batch(directory,io=IO)
            self.assertEqual(batch['actions'],[COMMAND]);self.assertEqual(batch['io'],IO)
            self.assertIsInstance(batch['observations'],list)

    def test_unbounded_native_limit_and_empty_dynamic_arrays_are_not_invented_limits_or_values(self):
        io=deepcopy(IO);io['ports']['arm']['limits']=[[None,None]]*3
        with tempfile.TemporaryDirectory() as directory:
            recorder=records.Recorder(directory,io=io);initial=observation(0,truth=True)
            initial['streams']['nodes']=np.empty((0,3),dtype=np.float32)
            recorder.begin({'observation':observation(0),'truth':initial},Scorer())
            after=observation(.02,truth=True);after['streams']['nodes']=np.empty((0,3),dtype=np.float32)
            command={'arm':[100.,200.,300.]}
            recorder.commit(observation(0),command,command,observation(.02),10,truth_before=initial,truth_after=after)
            recorder.close('completed');batch=records.load_batch(directory,io=io)
            self.assertEqual(batch['actions'],[command])
            reference=batch['transitions'][0]['truthAfter']['streams']['nodes']
            self.assertEqual(records.read_array(directory,reference).shape,(0,3))

    def test_physics_rate_samples_have_actual_substep_count_and_final_source_boundary(self):
        io=deepcopy(IO);io['streams']['q']['rateHz']=500
        with tempfile.TemporaryDirectory() as directory:
            recorder,_=begin(directory,io)
            with self.assertRaisesRegex(ValueError,'substep'):
                recorder.commit(observation(0),COMMAND,COMMAND,observation(.02),10,
                    truth_before=observation(0,truth=True),truth_after=observation(.02,truth=True))
            with self.assertRaisesRegex(RuntimeError,'failed'):
                recorder.commit(observation(0),COMMAND,COMMAND,observation(.02),10)
            recorder.close('interrupted')
        with tempfile.TemporaryDirectory() as directory:
            recorder,_=begin(directory,io)
            samples=[observation(i/500) for i in range(1,11)]
            recorder.commit(observation(0),COMMAND,COMMAND,observation(.02),10,
                truth_before=observation(0,truth=True),truth_after=observation(.02,truth=True),physical_samples=samples)
            recorder.close('completed');rows,_,_=records.read_transitions(directory)
            self.assertEqual([sample['physicsStep'] for sample in rows[0]['physicalSamples']],list(range(1,11)))

    def test_intermediate_rate_streams_cannot_omit_physical_samples(self):
        io,before,truth_before,after,truth_after,_=sampled_cycle(50)
        with tempfile.TemporaryDirectory() as directory:
            recorder=records.Recorder(directory,io=io)
            recorder.begin({'observation':before,'truth':truth_before},Scorer())
            with self.assertRaisesRegex(ValueError,'substep'):
                recorder.commit(before,COMMAND,COMMAND,after,4,truth_before=truth_before,truth_after=truth_after)
            recorder.close('interrupted')

    def test_stream_sampling_requires_the_same_integer_divisors_as_native_runtime(self):
        for rate in (0,75,100.):
            with self.subTest(rate=rate):
                io=sampled_cycle(50)[0];io['streams']['q']['rateHz']=rate
                with self.assertRaisesRegex(ValueError,'sampling'):records.validate_io(io)

    def test_intermediate_rate_samples_keep_due_ticks_and_held_final_boundary(self):
        for control_rate in (50,40):
            with tempfile.TemporaryDirectory() as directory,self.subTest(control_rate=control_rate):
                io,before,truth_before,_,_,_=sampled_cycle(control_rate)
                recorder=records.Recorder(directory,io=io)
                recorder.begin({'observation':before,'truth':truth_before},Scorer())
                for start in (0,200//control_rate):
                    _,before,truth_before,after,truth_after,samples=sampled_cycle(control_rate,start)
                    recorder.commit(before,COMMAND,COMMAND,after,len(samples),truth_before=truth_before,
                        truth_after=truth_after,physical_samples=samples)
                recorder.close('completed');rows,_,_=records.read_transitions(directory)
                for row in rows:
                    for sample in row['physicalSamples']:
                        self.assertEqual(set(sample['streams']),{'q'} if sample['physicsStep']%2==0 else set())
                    last=next(sample for sample in reversed(row['physicalSamples']) if 'q' in sample['streams'])
                    self.assertEqual(row['truthAfter']['streams']['q'],last['streams']['q'])
                    self.assertEqual(row['truthAfter']['streamStatus']['q'],last['streamStatus']['q'])
                if control_rate==40:
                    self.assertEqual(rows[0]['physicalSamples'][-1]['physicsStep'],5)
                    self.assertEqual(rows[0]['truthAfter']['streamStatus']['q']['physicsStep'],4)

    def test_intermediate_rate_missing_extra_stale_or_boundary_mismatched_samples_are_rejected(self):
        for kind in ('missing_due','off_due','stale_due','boundary_value','boundary_sample_clock'):
            with tempfile.TemporaryDirectory() as directory,self.subTest(kind=kind):
                io,before,truth_before,after,truth_after,samples=sampled_cycle(40)
                if kind=='missing_due':samples[1]['streams']={};samples[1]['streamStatus']={}
                elif kind=='off_due':samples[0]['streams']=deepcopy(samples[1]['streams']);samples[0]['streamStatus']={}
                elif kind=='stale_due':samples[1]['streamStatus']['q']={'status':'available','sampleTime':0.,'physicsStep':0}
                elif kind=='boundary_value':
                    for value in (after,truth_after):value['streams']['q'][0]+=.001
                else:
                    for value in (after,truth_after):value['streamStatus']['q']={'status':'available','sampleTime':0.,'physicsStep':0}
                recorder=records.Recorder(directory,io=io)
                recorder.begin({'observation':before,'truth':truth_before},Scorer())
                with self.assertRaises(ValueError):
                    recorder.commit(before,COMMAND,COMMAND,after,5,truth_before=truth_before,
                        truth_after=truth_after,physical_samples=samples)
                self.assertTrue(recorder.failed);self.assertEqual(recorder.cycles,0)
                self.assertEqual(recorder.committed_observation,before)
                recorder.close('interrupted')





if __name__=='__main__':unittest.main()

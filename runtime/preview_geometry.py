"""Tessellate native USD primitives only in a derived browser-export stage."""
from pathlib import Path

import numpy as np
import trimesh


def _shape_mesh(prim):
    from pxr import UsdGeom
    kind = str(prim.GetTypeName())
    if kind == 'Cube':
        size = float(UsdGeom.Cube(prim).GetSizeAttr().Get())
        mesh = trimesh.creation.box(extents=[size]*3)
        normals = np.repeat(mesh.face_normals[:, None, :], 3, axis=1)
        parameters = {'size': size}
    elif kind in ('Cylinder', 'Cone'):
        shape = UsdGeom.Cylinder(prim) if kind == 'Cylinder' else UsdGeom.Cone(prim)
        radius, height = float(shape.GetRadiusAttr().Get()), float(shape.GetHeightAttr().Get())
        axis = str(shape.GetAxisAttr().Get())
        if kind == 'Cylinder':
            mesh = trimesh.creation.cylinder(radius=radius, height=height, sections=128)
            normals = np.repeat(mesh.face_normals[:, None, :], 3, axis=1)
            sides = np.abs(mesh.face_normals[:, 2]) < .5
            radial = mesh.vertices[mesh.faces[sides]].copy()
            radial[:, :, 2] = 0
            normals[sides] = radial/np.linalg.norm(radial, axis=2, keepdims=True)
        else:
            mesh = trimesh.creation.cone(radius=radius, height=height, sections=128)
            mesh.vertices[:, 2] -= height/2
            normals = np.repeat(mesh.face_normals[:, None, :], 3, axis=1)
        if axis != 'Z':
            indices = {'X': [2, 0, 1], 'Y': [1, 2, 0]}[axis]
            mesh.vertices = mesh.vertices[:, indices]
            normals = normals[:, :, indices]
        parameters = {'radius': radius, 'height': height, 'axis': axis, 'radialSegments': 128}
    else:
        raise ValueError('unsupported visible native preview primitive: '+kind+' '+str(prim.GetPath()))
    return mesh, normals, parameters


def prepare(usd_path, destination):
    from pxr import Gf, Usd, UsdGeom
    stage = Usd.Stage.Open(str(usd_path))
    source_prims = [prim for prim in Usd.PrimRange.Stage(stage, Usd.TraverseInstanceProxies())
                    if prim.IsA(UsdGeom.Gprim)
                    and UsdGeom.Imageable(prim).ComputeVisibility() != 'invisible'
                    and UsdGeom.Imageable(prim).ComputePurpose() in ('default', 'render')]
    shapes = [prim for prim in source_prims if not prim.IsA(UsdGeom.Mesh)]
    inventory = [{'path': str(prim.GetPath()), 'type': str(prim.GetTypeName())} for prim in source_prims]
    if not shapes:
        return Path(usd_path), {'sourcePrimitives': inventory, 'tessellatedPrimitives': []}
    stage.SetEditTarget(stage.GetSessionLayer())
    # Flattening live instances creates prototype roots that the GLB converter
    # can export as extra scenes. Expand only in this anonymous session first.
    while instances := [prim for prim in Usd.PrimRange.Stage(stage) if prim.IsInstance()]:
        for prim in instances:
            prim.SetInstanceable(False)
    records = []
    for row in inventory:
        if row['type'] == 'Mesh':
            continue
        prim = stage.GetPrimAtPath(row['path'])
        mesh, normals, parameters = _shape_mesh(prim)
        before = np.asarray(UsdGeom.XformCache().GetLocalToWorldTransform(prim)).copy()
        target = UsdGeom.Mesh.Define(stage, prim.GetPath())
        target.CreatePointsAttr([Gf.Vec3f(*point) for point in mesh.vertices])
        target.CreateFaceVertexCountsAttr([3]*len(mesh.faces))
        target.CreateFaceVertexIndicesAttr(mesh.faces.reshape(-1).tolist())
        target.CreateNormalsAttr([Gf.Vec3f(*normal) for normal in normals.reshape(-1, 3)])
        target.SetNormalsInterpolation('faceVarying')
        target.CreateSubdivisionSchemeAttr('none')
        target.CreateExtentAttr([Gf.Vec3f(*bound) for bound in mesh.bounds])
        after = np.asarray(UsdGeom.XformCache().GetLocalToWorldTransform(target.GetPrim()))
        if not np.array_equal(before, after):
            raise ValueError('preview tessellation changed native world transform: '+row['path'])
        records.append({**row, 'parameters': parameters, 'triangles': len(mesh.faces)})
    stage.Flatten().Export(str(destination))
    return Path(destination), {'sourcePrimitives': inventory, 'tessellatedPrimitives': records}

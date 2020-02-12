__author__ = 'Martijn Berger'
__license__ = "GPL"

# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see <http://www.gnu.org/licenses/>.

bl_info = {
    "name": "Sketchup importer",
    "author": "Martijn Berger",
    "version": (0, 1, 5, 'dev'),
    "blender": (2, 7, 6),
    "description": "import/export Sketchup skp files",
    "warning": "Very early preview",
    "wiki_url": "https://github.com/martijnberger/pyslapi",
    "tracker_url": "",
    "category": "Import-Export",
    "location": "File > Import"}

import bpy
import os
import time
from . import sketchup
import tempfile
from math import pi
from mathutils import Matrix, Vector, Quaternion
from bpy.types import Operator, AddonPreferences
from bpy.props import StringProperty, IntProperty, BoolProperty, EnumProperty
from bpy_extras.io_utils import ImportHelper, unpack_list, unpack_face_list, ExportHelper
from extensions_framework import log
from .SKPutil import *


class SketchupAddonPreferences(AddonPreferences):
    bl_idname = __name__

    camera_far_plane = IntProperty(name="Default Camera Distance", default=1250)
    draw_bounds = IntProperty(name="Draw object as bounds when over", default=5000)


    def draw(self, context):
        layout = self.layout
        layout.label(text="SKP import options:")
        layout.prop(self, "camera_far_plane")
        layout.prop(self, "draw_bounds")



def sketchupLog(*args):
    if len(args) > 0:
        log(' '.join(['%s'%a for a in args]), module_name='Sketchup')


class SceneImporter():
    def __init__(self):
        self.filepath = '/tmp/untitled.skp'
        self.name_mapping = {}
        self.component_meshes = {}
        self.scene = None
        self.layers_skip = []

    def set_filename(self, filename):
        self.filepath = filename
        self.basepath, self.skp_filename = os.path.split(self.filepath)
        return self # allow chaining

    def load(self, context, **options):
        """load a sketchup file"""
        self.context = context
        self.reuse_material = options['reuse_material']
        self.reuse_group = options['reuse_existing_groups']
        self.max_instance = options['max_instance']
        self.component_stats = defaultdict(list)
        self.component_skip = proxy_dict()
        self.component_depth = proxy_dict()
        self.group_written ={}
        self.aspect_ratio = context.scene.render.resolution_x / context.scene.render.resolution_y

        sketchupLog('importing skp %r' % self.filepath)

        addon_name = __name__.split('.')[0]
        self.prefs = context.user_preferences.addons[addon_name].preferences

        time_main = time.time()

        try:
            self.skp_model = sketchup.Model.from_file(self.filepath)
        except Exception as e:
            sketchupLog('Error reading input file: %s' % self.filepath)
            sketchupLog(e)
            return {'FINISHED'}


        if options['import_scene']:
            for s in self.skp_model.scenes:
                if s.name == options['import_scene']:
                    sketchupLog("Importing Scene \"{}\" ".format(s.name))
                    self.scene = s
                    self.layers_skip = [l for l in s.layers]
                    for l in s.layers:
                        sketchupLog("SKIP: {}".format(l.name))
            if not self.layers_skip:
                sketchupLog('Could not find scene {} importing default'.format(options['import_scene']))

        if not self.layers_skip:
            self.layers_skip = [l for l in self.skp_model.layers if not l.visible]

        sketchupLog('Skipping layers: ')
        for l in sorted([l.name for l in self.layers_skip]):
            sketchupLog(l)

        self.skp_components = proxy_dict(self.skp_model.component_definition_as_dict)

        sketchupLog('parsed skp %r in %.4f sec.' % (self.filepath, (time.time() - time_main)))

        if options['scenes_as_camera']:
            for s in self.skp_model.scenes:
                self.write_camera(s.camera, s.name)

        if options['import_camera']:
            if self.scene:
                active_cam = self.write_camera(self.scene.camera, name=self.scene.name)
                context.scene.camera = active_cam
            else:
                active_cam = self.write_camera(self.skp_model.camera)
                context.scene.camera = active_cam

        t1 = time.time()
        self.write_materials(self.skp_model.materials)
        sketchupLog('imported materials in %.4f sec' % (time.time() - t1))

        t1 = time.time()
        D = SKP_util()
        SKP_util.layers_skip = self.layers_skip

        for c in self.skp_model.component_definitions:
            self.component_depth[c.name] = D.component_deps(c.entities)
        sketchupLog('analyzed component depths in %.4f sec' % (time.time() - t1))


        self.write_duplicateable_groups()

        if options["dedub_only"]:
            return {'FINISHED'}

        self.component_stats = defaultdict(list)
        self.write_entities(self.skp_model.entities, "Sketchup", Matrix.Identity(4))

        for k, v in self.component_stats.items():
            name, mat = k
            if options['dedub_type'] == "VERTEX":
                self.instance_group_dupli_vert(name, mat, self.component_stats)
            else:
                self.instance_group_dupli_face(name, mat, self.component_stats)

        sketchupLog('imported entities in %.4f sec' % (time.time() - t1))

        sketchupLog('finished importing %s in %.4f sec '% (self.filepath, time.time() - time_main))
        return {'FINISHED'}


    def write_duplicateable_groups(self):
        component_stats = self.analyze_entities(self.skp_model.entities, "Sketchup", Matrix.Identity(4), component_stats=defaultdict(list))
        instance_when_over = self.max_instance
        max_depth = max(self.component_depth.values(), default=0)
        component_stats = { k : v for k,v in component_stats.items() if len(v) >= instance_when_over }
        for i in range(max_depth + 1):
            for k, v in component_stats.items():
                name, mat = k
                depth = self.component_depth[name]
                #print(k, len(v), depth)
                comp_def = self.skp_components[name]
                if comp_def and depth == 1:
                    #self.component_skip[(name,mat)] = comp_def.entities
                    pass
                elif comp_def and depth == i:
                    gname = group_name(name,mat)
                    if self.reuse_group and gname in bpy.data.groups:
                        #print("Group {} already defined".format(gname))
                        self.component_skip[(name,mat)] = comp_def.entities
                        self.group_written[(name,mat)] = bpy.data.groups[gname]
                    else:
                        group = bpy.data.groups.new(name=gname)
                        #print("Component written as group".format(gname))
                        self.conponent_definition_as_group(comp_def.entities, name, Matrix(), default_material=mat, etype=EntityType.outer, group=group)
                        self.component_skip[(name,mat)] = comp_def.entities
                        self.group_written[(name,mat)] = group





    def analyze_entities(self, entities, name, transform, default_material="Material", etype=EntityType.none, component_stats=None, component_skip=[]):
        if etype == EntityType.component:
            component_stats[(name,default_material)].append(transform)

        for group in entities.groups:
            if self.layers_skip and group.layer in self.layers_skip:
                continue
            self.analyze_entities(group.entities,
                                  "G-" + group.name,
                                  transform * Matrix(group.transform),
                                  default_material=inherent_default_mat(group.material, default_material),
                                  etype=EntityType.group,
                                  component_stats=component_stats)

        for instance in entities.instances:
            if self.layers_skip and instance.layer in self.layers_skip:
                continue
            mat = inherent_default_mat(instance.material, default_material)
            cdef = self.skp_components[instance.definition.name]
            if (cdef.name,mat) in component_skip:
                continue
            self.analyze_entities(cdef.entities,
                                  cdef.name,
                                  transform * Matrix(instance.transform),
                                  default_material=mat,
                                  etype=EntityType.component,
                                  component_stats=component_stats)
        return component_stats

    def write_materials(self,materials):
        if self.context.scene.render.engine != 'CYCLES':
            self.context.scene.render.engine = 'CYCLES'

        self.materials = {}
        self.materials_scales = {}
        if self.reuse_material and 'Material' in bpy.data.materials:
            self.materials['Material'] = bpy.data.materials['Material']
        else:
            bmat = bpy.data.materials.new('Material')
            bmat.diffuse_color = (.8, .8, .8)
            bmat.use_nodes = True
            self.materials['Material'] = bmat


        for mat in materials:

            name = mat.name

            if mat.texture:
                self.materials_scales[name] = mat.texture.dimensions[2:]
            else:
                self.materials_scales[name] = (1.0, 1.0)



            if self.reuse_material and not name in bpy.data.materials:
                bmat = bpy.data.materials.new(name)
                r, g, b, a = mat.color
                tex = mat.texture
                if a < 255:
                    bmat.alpha = a / 256.0
                bmat.diffuse_color = (r / 256.0, g / 256.0, b / 256.0)
                bmat.use_nodes = True
                if tex:
                    tex_name = tex.name.split("\\")[-1]
                    tmp_name = os.path.join(tempfile.gettempdir() , tex_name)
                    #sketchupLog("Texture saved temporarily as {}".format(tmp_name))
                    tex.write(tmp_name)
                    img = bpy.data.images.load(tmp_name)
                    img.pack()
                    os.remove(tmp_name)
                    n = bmat.node_tree.nodes.new('ShaderNodeTexImage')
                    n.image = img
                    bmat.node_tree.links.new(n.outputs['Color'], bmat.node_tree.nodes['Diffuse BSDF'].inputs['Color'] )

                    #pax, que sea principled BSDF para poder exportar a unity sin problemas...
                    xMat = bmat.node_tree.nodes.new('ShaderNodeBsdfPrincipled')


                    # Remove default
                    bmat.node_tree.nodes.remove(bmat.node_tree.nodes.get('Diffuse BSDF'))
                    bmat_output = bmat.node_tree.nodes.get('Material Output')


                    bmat.node_tree.links.new(n.outputs['Color'], bmat.node_tree.nodes['Principled BSDF'].inputs['Base Color'] )
                    bmat.node_tree.links.new(bmat_output.inputs[0], xMat.outputs[0])

                self.materials[name] = bmat
            else:
                self.materials[name] = bpy.data.materials[name]



    def write_mesh_data(self, entities=None, name="", default_material='Material'):
        mesh_key = (name,default_material)
        if mesh_key in self.component_meshes:
            return self.component_meshes[mesh_key]
        verts = []
        faces = []
        mat_index = []
        mats = keep_offset()
        seen = keep_offset()
        uv_list = []
        alpha = False # We assume object does not need alpha flag
        uvs_used = False # We assume no uvs need to be added


        for f in entities.faces:

            if f.material:
                mat_number = mats[f.material.name]
            else:
                mat_number = mats[default_material]
                if default_material != 'Material':
                    f.st_scale = self.materials_scales[default_material]

            vs, tri, uvs = f.tessfaces

            mapping = {}
            for i, (v, uv) in enumerate(zip(vs, uvs)):
                l = len(seen)
                mapping[i] = seen[v]
                if len(seen) > l:
                    verts.append(v)
                uvs.append(uv)


            for face in tri:
                f0, f1, f2 = face[0], face[1], face[2]
                if mapping[f2] == 0 : ## eeekadoodle dance
                    faces.append( ( mapping[f2], mapping[f0], mapping[f1] ) )
                    uv_list.append(( uvs[f2][0], uvs[f2][1],
                                     uvs[f0][0], uvs[f0][1],
                                     uvs[f1][0], uvs[f1][1],
                                     0, 0 ) )
                else:
                    faces.append( ( mapping[f0], mapping[f1], mapping[f2] ) )
                    uv_list.append(( uvs[f0][0], uvs[f0][1],
                                     uvs[f1][0], uvs[f1][1],
                                     uvs[f2][0], uvs[f2][1],
                                     0, 0 ) )
                mat_index.append(mat_number)

        # verts, faces, uv_list, mat_index, mats = entities.get__triangles_lists(default_material)

        if len(verts) == 0:
            return None, False

        me = bpy.data.meshes.new(name)

        me.vertices.add(len(verts))
        me.tessfaces.add(len(faces))

        if len(mats) >= 1:
            mats_sorted = OrderedDict(sorted(mats.items(), key=lambda x: x[1]))
            for k in mats_sorted.keys():
                bmat = self.materials[k]
                me.materials.append(bmat)
                if bmat.alpha < 1.0:
                    alpha = True
                try:
                    if 'Image Texture' in bmat.node_tree.nodes.keys():
                        uvs_used = True
                except AttributeError as e:
                    uvs_used = False
        else:
            sketchupLog("WARNING OBJECT {} HAS NO MATERIAL".format(name))

        me.vertices.foreach_set("co", unpack_list(verts))
        me.tessfaces.foreach_set("vertices_raw", unpack_face_list(faces))
        me.tessfaces.foreach_set("material_index", mat_index)

        if uvs_used:
            me.tessface_uv_textures.new()
            for i in range(len(faces)):
                me.tessface_uv_textures[0].data[i].uv_raw = uv_list[i]

        me.update(calc_edges=True)
        me.validate()
        self.component_meshes[mesh_key] = me, alpha
        return me, alpha

    def write_entities(self, entities, name, parent_tranform, default_material="Material", etype=None):
        if etype == EntityType.component and (name,default_material) in self.component_skip:
            self.component_stats[(name,default_material)].append(parent_tranform)
            return

        me, alpha = self.write_mesh_data(entities=entities, name=name, default_material=default_material)

        if me:
            ob = bpy.data.objects.new(name, me)
            ob.matrix_world = parent_tranform
            if alpha > 0.01 and alpha < 1.0:
                ob.show_transparent = True
            me.update(calc_edges=True)
            self.context.scene.objects.link(ob)

        for group in entities.groups:
            if group.hidden:
                continue
            if self.layers_skip and group.layer in self.layers_skip:
                continue
            self.write_entities(group.entities,
                                "G-" + group_safe_name(group.name),
                                parent_tranform * Matrix(group.transform),
                                default_material=inherent_default_mat(group.material, default_material),
                                etype=EntityType.group)

        for instance in entities.instances:
            if instance.hidden:
                continue
            if self.layers_skip and instance.layer in self.layers_skip:
                continue
            mat_name = inherent_default_mat(instance.material, default_material)
            cdef = self.skp_components[instance.definition.name]
            self.write_entities(cdef.entities,
                                cdef.name,
                                parent_tranform * Matrix(instance.transform),
                                default_material=mat_name,
                                etype=EntityType.component)


    def instance_object_or_group(self, name, default_material):
        try:
            group = self.group_written[(name,default_material)]
            ob = bpy.data.objects.new(name=name, object_data=None)
            ob.dupli_type = 'GROUP'
            ob.dupli_group = group
            ob.empty_draw_size = 0.01
            return ob
        except KeyError as e:
            me, alpha = self.component_meshes[(name,default_material)]
            ob = bpy.data.objects.new(name, me)
            if alpha:
                ob.show_transparent = True
            me.update(calc_edges=True)
            return ob

    def conponent_definition_as_group(self, entities, name, parent_tranform, default_material="Material", etype=None, group=None):
        if etype == EntityType.outer:
            if (name, default_material) in self.component_skip:
                return
            else:
                sketchupLog("Write instance definition as group {} {}".format(group.name, default_material))
                self.component_skip[(name, default_material)] = True
        if etype == EntityType.component and (name,default_material) in self.component_skip:
            ob = self.instance_object_or_group(name,default_material)
            ob.matrix_world = parent_tranform
            self.context.scene.objects.link(ob)
            ob.layers = 18 * [False] + [True] + [False]
            group.objects.link(ob)
            return
        else:
            me, alpha = self.write_mesh_data(entities=entities, name=name, default_material=default_material)

        if me:
            ob = bpy.data.objects.new(name, me)
            ob.matrix_world = parent_tranform
            if alpha:
                ob.show_transparent = True
            me.update(calc_edges=True)
            self.context.scene.objects.link(ob)
            ob.layers = 18 * [False] + [True] + [False]
            group.objects.link(ob)

        for g in entities.groups:
            if self.layers_skip and g.layer in self.layers_skip:
                continue
            self.conponent_definition_as_group(g.entities,
                                "G-" + g.name,
                                parent_tranform * Matrix(g.transform),
                                default_material=inherent_default_mat(g.material, default_material),
                                etype=EntityType.group,
                                group=group)

        for instance in entities.instances:
            if self.layers_skip and instance.layer in self.layers_skip:
                continue
            cdef = self.skp_components[instance.definition.name]
            self.conponent_definition_as_group(cdef.entities,
                                cdef.name,
                                parent_tranform * Matrix(instance.transform),
                                default_material=inherent_default_mat(instance.material, default_material),
                                etype=EntityType.component,
                                group=group)





    def instance_group_dupli_vert(self, name, default_material, component_stats):
        def get_orientations(v):
            orientations = defaultdict(list)
            for transform in v:
                loc, rot, scale = Matrix(transform).decompose()
                scale = (scale[0], scale[1], scale[2])
                rot = (rot[0], rot[1], rot[2], rot[3])
                orientations[(scale, rot)].append((loc[0], loc[1], loc[2]))
            for key, locs in orientations.items():
                scale , rot = key
                yield scale , rot, locs

        for scale, rot, locs in get_orientations(component_stats[(name, default_material)]):
            verts = []
            main_loc = Vector(locs[0])
            for c in locs:
                verts.append(Vector(c) - main_loc)
            dme = bpy.data.meshes.new('DUPLI_' + name)
            dme.vertices.add(len(verts))
            dme.vertices.foreach_set("co", unpack_list(verts))
            dme.update(calc_edges=True) # Update mesh with new data
            dme.validate()
            dob = bpy.data.objects.new("DUPLI_" + name, dme)
            dob.location = main_loc
            dob.dupli_type = 'VERTS'

            ob = self.instance_object_or_group(name,default_material)
            ob.scale = scale
            ob.rotation_quaternion = Quaternion((rot[0], rot[1], rot[2], rot[3]))
            ob.parent = dob

            self.context.scene.objects.link(ob)
            self.context.scene.objects.link(dob)
            sketchupLog("Complex group {} {} instanced {} times, scale -> {}".format(name, default_material, len(verts), scale))
        return

    def instance_group_dupli_face(self, name, default_material, component_stats):
        def get_orientations( v):
            orientations = defaultdict(list)
            for transform in v:
                loc, rot, scale = Matrix(transform).decompose()
                scale = (scale[0], scale[1], scale[2])
                orientations[scale].append(transform)
            for scale, transforms in orientations.items():
                yield scale, transforms


        for scale, transforms in get_orientations(component_stats[(name, default_material)]):
            main_loc, _, real_scale  = Matrix(transforms[0]).decompose()
            verts = []
            faces = []
            f_count = 0
            for c in transforms:
                l_loc, l_rot, l_scale = Matrix(c).decompose()
                mat = Matrix.Translation(l_loc) * l_rot.to_matrix().to_4x4()
                verts.append(Vector((mat * Vector((-0.05, -0.05, 0, 1.0)))[0:3]) - main_loc)
                verts.append(Vector((mat * Vector(( 0.05, -0.05, 0, 1.0)))[0:3]) - main_loc)
                verts.append(Vector((mat * Vector(( 0.05,  0.05, 0, 1.0)))[0:3]) - main_loc)
                verts.append(Vector((mat * Vector((-0.05,  0.05, 0, 1.0)))[0:3]) - main_loc)
                faces.append( (f_count + 0,  f_count + 1, f_count + 2, f_count + 3) )
                f_count += 4
            dme = bpy.data.meshes.new('DUPLI_' + name)
            dme.vertices.add(len(verts))
            dme.vertices.foreach_set("co", unpack_list(verts))

            dme.tessfaces.add(f_count /4 )
            dme.tessfaces.foreach_set("vertices_raw", unpack_face_list(faces))
            dme.update(calc_edges=True) # Update mesh with new data
            dme.validate()
            dob = bpy.data.objects.new("DUPLI_" + name, dme)
            dob.dupli_type = 'FACES'
            dob.location = main_loc
            #dob.use_dupli_faces_scale = True
            #dob.dupli_faces_scale = 10

            ob = self.instance_object_or_group(name,default_material)
            ob.scale = real_scale
            ob.parent = dob
            self.context.scene.objects.link(ob)
            self.context.scene.objects.link(dob)
            sketchupLog("Complex group {} {} instanced {} times".format(name, default_material, f_count / 4))
        return

    def write_camera(self, camera, name="Active Camera"):
        pos, target, up = camera.GetOrientation()
        bpy.ops.object.add(type='CAMERA', location=pos)
        ob = self.context.object
        ob.name = name

        z = (Vector(pos) - Vector(target))
        x = Vector(up).cross(z)
        y = z.cross(x)

        x.normalize()
        y.normalize()
        z.normalize()

        ob.matrix_world.col[0] = x.resized(4)
        ob.matrix_world.col[1] = y.resized(4)
        ob.matrix_world.col[2] = z.resized(4)

        cam = ob.data
        aspect_ratio = camera.aspect_ratio
        fov = camera.fov
        if aspect_ratio == False: # we seem to be using dynamic / screen aspect ratio
            sketchupLog("CAMERA {} uses dynamic / screen aspect ratio ".format(name))
            aspect_ratio = self.aspect_ratio
        if fov == False:
            sketchupLog("CAMERA {} is ortho ".format(name))
            cam.type = 'ORTHO'
        else:
            cam.angle = (pi * fov / 180 ) * aspect_ratio
        cam.clip_end = self.prefs.camera_far_plane
        cam.name = name



class SceneExporter():
    def __init__(self):
        self.filepath = '/tmp/untitled.skp'

    def set_filename(self, filename):
        self.filepath = filename
        self.basepath, self.skp_filename = os.path.split(self.filepath)
        return self # allow chaining

    def save(self, context, **options):
        sketchupLog('finished exporting %s'% (self.filepath))
        return {'FINISHED'}


class ImportSKP(bpy.types.Operator, ImportHelper):
    """load a Trimble Sketchup SKP file"""
    bl_idname = "import_scene.skp"
    bl_label = "Import SKP"
    bl_options = {'PRESET', 'UNDO'}

    filename_ext = ".skp"

    filter_glob = StringProperty(
        default="*.SKP",
        options={'HIDDEN'},
    )

    import_camera = BoolProperty(name="Camera", description="Import Active view as camera", default=True)
    reuse_material = BoolProperty(name="Use Existing Materials", description="Reuse scene materials", default=True)
    max_instance = IntProperty( name="Create DUPLI faces instance when count over", default=50)
    dedub_type = EnumProperty(
            name="Deduplication Type",
            items=(('FACE', "Dupli Faces", ""),
                   ('VERTEX', "Dupli Verts", ""),
                   ),
            default='FACE',
            )
    dedub_only = BoolProperty(name="Groups Only", description="Import deduplicated groups only", default=False)
    scenes_as_camera = BoolProperty(name="Scenes", description="Import Active view as camera", default=True)
    import_scene = StringProperty(name="Import Scene", description="Name of the Sketchup scene to import", default="")
    reuse_existing_groups = BoolProperty(name="Reuse groups", description="Use existing blender groups to instance componenets with", default=False)

    def execute(self, context):
        keywords = self.as_keywords(ignore=("axis_forward",
                                            "axis_up",
                                            "filter_glob",
                                            "split_mode"))
        return SceneImporter().set_filename(keywords['filepath']).load(context, **keywords)

    def draw(self, context):
        layout = self.layout

        row = layout.row(align=True)
        row.prop(self, "import_camera")
        row.prop(self, "reuse_material")
        row = layout.row(align=True)
        row.prop(self, "max_instance")
        row.prop(self, "dedub_type")
        row = layout.row(align=True)
        row.prop(self, "scenes_as_camera")
        row.prop(self, "dedub_only")
        row = layout.row(align=True)
        row.prop(self, "import_scene")
        row.prop(self, "reuse_existing_groups")


class ExportSKP(bpy.types.Operator, ExportHelper):
    """load a Trimble Sketchup SKP file"""
    bl_idname = "export_scene.skp"
    bl_label = "Export SKP"
    bl_options = {'PRESET', 'UNDO'}

    filename_ext = ".skp"

    def execute(self, context):
        keywords = self.as_keywords()
        return SceneExporter().set_filename(keywords['filepath']).save(context, **keywords)


def menu_func_import(self, context):
    self.layout.operator(ImportSKP.bl_idname, text="Import Sketchup Scene(.skp)")

def menu_func_export(self, context):
    self.layout.operator(ExportSKP.bl_idname, text="Export Sketchup Scene(.skp)")

# Registration
def register():
    bpy.utils.register_class(SketchupAddonPreferences)
    bpy.utils.register_class(ImportSKP)
    bpy.types.INFO_MT_file_import.append(menu_func_import)
    bpy.utils.register_class(ExportSKP)
    bpy.types.INFO_MT_file_export.append(menu_func_export)


def unregister():
    bpy.utils.unregister_class(ImportSKP)
    bpy.types.INFO_MT_file_import.remove(menu_func_import)
    bpy.utils.unregister_class(ExportSKP)
    bpy.types.INFO_MT_file_export.remove(menu_func_export)
    bpy.utils.unregister_class(SketchupAddonPreferences)

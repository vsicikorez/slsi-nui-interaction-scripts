#The Sign Language Synthesis and Interaction Research Tools
#    Copyright (C) 2014  Fabrizio Nunnari, Alexis Heloir, DFKI
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.

import bpy
import bgl
import blf
import mathutils

from bpy.props import * # for properties

import os
from bpy_extras import image_utils

import time # for real-time animation

import re

from LeapNUI.LeapReceiver import LeapReceiver
from LeapNUI.LeapReceiver import PointableSelector
from LeapNUI.LeapReceiver import HandSelector

from MakeHumanTools.BoneSet import MH_HAND_BONES_L
from MakeHumanTools.BoneSet import MH_HAND_BONES_R
from MakeHumanTools.BoneSet import MH_HAND_CONTROLLERS_L
from MakeHumanTools.BoneSet import MH_HAND_CONTROLLERS_R

LHAND_ACTIVATION_CHAR = 'D'
RHAND_ACTIVATION_CHAR = 'A'

LHAND_POSE_LIBRARY_NAME = "handshape_lib_L"
RHAND_POSE_LIBRARY_NAME = "handshape_lib_R"

# Delay between TIMER events when entering the modal command mode. In seconds.
UPDATE_DELAY = 0.04

#SELECTION_MAX_Y = 250
#SELECTION_MIN_Y = 100
#SELECTION_MAX_Y = 210
#SELECTION_MIN_Y = 190

SELECTION_STABLE_RANGE = 10     # (mm) The size of the stable (non scroll) y range, over and below the initial finger y.


# The proportion of display area that will be occupied by the transparent background.
SELECTION_DISPLAY_HEIGHT = 0.8

MAX_DISPLAY_ELEMENTS = 2


def getSelectedArmature(context):
    """Returns the selected armature. Or None"""
    
    arm = None
    
    objs = context.selected_objects
    if(len(objs) != 1):
        return None
    
    arm = objs[0]
    if(arm.type != "ARMATURE"):
        return None
    
    return arm


class HandShapeSelector(bpy.types.Operator):
    """This operator activates a interactive selection of a hand shape to impose to the character.
    It is a modal operator relying on LeapMotion data to control the selection."""
    
    bl_idname = "object.leap_hand_shape_selector"
    bl_label = "Select Hand Shape"
    bl_space_type = "VIEW_3D"
    bl_region_type = "TOOL_PROPS"
    bl_options = {'REGISTER', 'UNDO'}
    
    use_right_hand = BoolProperty(name="use_right_hand", description="Whether to operate on the right hand, or the left one", default=True)
    
    # Reference to the LeapReceiver singleton, to get updated leap dictionaries
    leap_receiver = None

    # Utility object to get the most stable last used pointable.
    pointable_selector = None

    # Utility object to get the most stable last used hand.
    hand_selector = None


    # The list of items to select
    selectable_items = []
    

    # Set to True in modal() if a finger (or hand) is visible and within the vertical range
    selector_visible = False
    

    # The finger height (y) detected at the first invocation of the oprtator. It will be atken as the center of the stability range.
    central_finger_y = None

    # A normalized float factor of the y position of the finger, in range [0,1). Set in modal().
    normalized_finger_y = 0
    
    # The number of the first item to select in the selection window
    # range [0, len(selectable_items) - MAX_DISPLAY_ELEMENTS - 1]
    selection_window_first_item = 0

    # The selection number for the highlighted item, in range [0,len(selectable_items)-1]
    selection_num = -1
    
    # Store rotations to later be able to restore original values.
    hand_initial_rotations = None

    # We want to draw the information only in the area/space/region where the function has been activated.
    # So, in this variable we will store the reference to the space (bpy.context.area.spaces.active) that was active when the user activated the controls.
    execution_active_space = None

    
    def __init__(self):
        self.leap_receiver = LeapReceiver.getSingleton()
        pass
    
    def __del__(self):
        self.removeHandlers()
        if(self.leap_receiver != None):
            LeapReceiver.releaseSingleton()
            self.leap_receiver = None
        # The sock attribute might not have been defined if the command was never run
        #if(hasattr(self, 'leap_receiver')):
        #    self.stop_leap_receiver()
        pass

    
    def addHandlers(self, context):
        #
        # TIMER
        self._timer = context.window_manager.event_timer_add(UPDATE_DELAY, context.window)

        #
        # DRAW
        self._draw_handle = bpy.types.SpaceView3D.draw_handler_add(self.draw_callback_px, (context,), 'WINDOW', 'POST_PIXEL')
        if(bpy.context.area):
            bpy.context.area.tag_redraw()



    _timer = None
    _draw_handle = None


    def removeHandlers(self):
        print("Removing handlers...")
        
        #
        # TIMER
        if(self._timer != None):
            self._timer = bpy.context.window_manager.event_timer_remove(self._timer)
            self._timer = None

        #
        # DRAW
        if(self._draw_handle != None):
            bpy.types.SpaceView3D.draw_handler_remove(self._draw_handle, 'WINDOW')
            self._draw_handle = None
            if(bpy.context.area):
                bpy.context.area.tag_redraw()

    
    def invoke(self, context, event):
        # Maybe here we check on which hand we have to apply the pose
        return self.execute(context)
    

    def execute(self, context):
        
        self.selected_armature = getSelectedArmature(context)
        if(self.selected_armature==None):
            self.report({'ERROR'}, "No selected armature")
            return {"CANCELLED"}
        
        if(self.use_right_hand):
            self.POSE_LIBRARY_NAME = RHAND_POSE_LIBRARY_NAME
            self.HAND_BONE_NAMES = MH_HAND_BONES_R
            self.controller_names = MH_HAND_CONTROLLERS_R
        else:
            self.POSE_LIBRARY_NAME = LHAND_POSE_LIBRARY_NAME
            self.HAND_BONE_NAMES = MH_HAND_BONES_L
            self.controller_names = MH_HAND_CONTROLLERS_L

        if(not self.POSE_LIBRARY_NAME in bpy.data.actions):
            self.report({'ERROR'}, "No action library named '" + self.POSE_LIBRARY_NAME + "' found")
            return {"CANCELLED"}

        self.hand_initial_rotations = retrieveBoneRotations(self.selected_armature, self.HAND_BONE_NAMES)


        # Store the initial rotation fo the controllers
        self.finger_controllers_initial_rots = retrieveBoneRotations(self.selected_armature, self.controller_names)

        # Reset the controllers to identity rotation (default position)
        resetFingerControllers(armature=self.selected_armature, controller_names=self.controller_names, try_record=False) 


        # Store the reference to the space where this command was activated. So that we render only there.
        self.execution_active_space = context.area.spaces.active
        
        # Retrieve entries from the pose library
        action = bpy.data.actions[self.POSE_LIBRARY_NAME]
        # See http://www.blender.org/documentation/blender_python_api_2_69_3/bpy.types.TimelineMarker.html?highlight=timelinemarker#bpy.types.TimelineMarker
        self.selectable_items = []
        for marker in action.pose_markers:
            print("Found marker " + str(marker.name) + " at frame " + str(marker.frame))
            self.selectable_items.append(marker.name)
            

        self.pointable_selector = PointableSelector()
        self.hand_selector = HandSelector()

        context.window_manager.modal_handler_add(self)
        self.addHandlers(context)


        return {"RUNNING_MODAL"}
    

    last_modal_time = None



    def modal(self, context, event):
        #FINISHED, CANCELLED, RUNNING_MODAL
        #print("HandShapeSelector running modal")
        
        now = time.time()
        if(self.last_modal_time == None):
            self.last_modal_time = now - UPDATE_DELAY
        dt = now - self.last_modal_time
        self.last_modal_time = now
        
        if event.type == 'ESC':
            # Restore hand rotations
            applyBoneRotations(self.selected_armature, self.hand_initial_rotations, try_record=False)
            applyBoneRotations(self.selected_armature, self.finger_controllers_initial_rots, try_record=False)
            return self.cancel(context)
        
        if (event.type == RHAND_ACTIVATION_CHAR or event.type == LHAND_ACTIVATION_CHAR) and event.value == "PRESS":
            n = int(self.selection_num)
            self.removeHandlers()
            self.stop_leap_receiver()
            if(n<0 or n>len(self.selectable_items)):
                self.report({'INFO'}, "No pose selected")
                return {"CANCELLED"}
            else:
                selection_name = self.selectable_items[n]
                applyPose(armature=self.selected_armature, pose_library_name=self.POSE_LIBRARY_NAME, hand_bone_names=self.HAND_BONE_NAMES, pose_name=selection_name, try_record=True)
                resetFingerControllers(armature=self.selected_armature, controller_names=self.controller_names, try_record=True) 
                return {"FINISHED"}
        
        leap_dict = self.leap_receiver.getLeapDict()
        if(leap_dict == None):
            print("No dictionary yet...")
            return {"RUNNING_MODAL"}


        if(bpy.context.window_manager.leap_hand_shape_selector_finger_extension_filter):
            FINGER_EXTENSION_FILTER = True
            FINGER_TIP_SELECTION = False
        else:
            FINGER_EXTENSION_FILTER = False
            FINGER_TIP_SELECTION = True

            
        #print(str(leap_dict))
        p = self.pointable_selector.select(leap_dict)
        h = self.hand_selector.select(leap_dict)
        #print("Current pointable = " + str(p))
        #print("Current hand = " + str(h))

        
        # If the pointable is valid. Calculate the selected value according to its height
        if( (FINGER_TIP_SELECTION and p==None) or ((not FINGER_TIP_SELECTION) and h==None)):
            self.selector_visible = False
        else:
            self.selector_visible = True

            #FINGER_TIP_SELECTION => p!=None
            #(not FINGER_TIP_SELECTION => h!=None)

            #
            # ITEMS FILTERING (USING FINGERS EXTENSION)
            #

            # Update the list of available items
            if(FINGER_EXTENSION_FILTER and h!=None):
                self.selectable_items = []
                # Retrieve entries from the pose library
                action = bpy.data.actions[self.POSE_LIBRARY_NAME]
                for marker in action.pose_markers:
                    #print(marker)
                    flags = getHandBitFlag(h['id'], leap_dict)
                    #print("{0:b}".format(flags))
                    #print("Found marker " + str(marker.name) + " at frame " + str(marker.frame))


                    skip = False
                    
                    # if the pose is in the finger OPEN db, the finger MUST be open. if it is not, we skip the pose.
                    if(marker.name in finger_open_db):
                        need_open_flags = finger_open_db[marker.name]
                        #print(marker.name+" ->\t{0:b}".format(need_open_flags))
                        # if some flag is missing, skip it!
                        if( (need_open_flags & flags) != need_open_flags):
                            skip = True

                    # if the pose is in the finger CLOSE db, the finger MUST be close. if it is not, we skip the pose.
                    if(marker.name in finger_closed_db):
                        need_close_flags = finger_closed_db[marker.name]
                        if( (need_close_flags & (~flags)) != need_close_flags):
                            skip = True

                    # if(marker.name in pinch_needed_set):
                    #     if((flags & 0b100000) != 0b100000):
                    #         skip = True

                    if(not skip):
                        self.selectable_items.append(marker.name)


            #
            # BASIC DATA
            #

            n_items = len(self.selectable_items)
            
            selection_stable_range = SELECTION_STABLE_RANGE
            if(FINGER_TIP_SELECTION):
                # use finger coordinates
                y = p['tipPosition'][1]
            else:
                # Use palm coordinates
                y = h['palmPosition'][1]
                # When using the palm, the precision is much lower. Increase the stability zone.
                selection_stable_range *= 3.0

            if(self.central_finger_y == None):
                self.central_finger_y = y

            # The finger offset with respect to the central initial y
            offset_y = y - self.central_finger_y
            normalized_offset_y = offset_y / selection_stable_range

            # remodulate from range [-1,1] to range [0,1]
            self.normalized_finger_y = (normalized_offset_y + 1) / 2
            #print(str(y) + "\toffy="+str(offset_y)+"\tnorm_finger_y="+str(self.normalized_finger_y))


            #
            # Check for CIRCLE GESTURE to shift the selection window
            #
            if(p!=None):

                p_id = p['id']
                gestures = leap_dict["gestures"]
                found_gesture = None
                for gesture in gestures:
                    if(gesture["type"] != "circle"):
                        continue
                    if(p_id in gesture["pointableIds"]):
                        found_gesture = gesture
                        break

                if(found_gesture != None):
                    #use tangent speed
                    vx,vy,vz = p['tipVelocity']
                    velocity = mathutils.Vector((vx,vy,vz))
                    linear_velocity = velocity.length
                    delta_scroll = 0.02 * linear_velocity * dt

                        
                    normal = gesture["normal"]
                    if(normal[2] < 0):
                        clockwise = True
                        self.selection_window_first_item += delta_scroll
                    else:
                        clockwise = False
                        self.selection_window_first_item -= delta_scroll
                        
                        
                    #print("Circling. Clockwise="+str(clockwise))
                    if(self.selection_window_first_item<0):
                        self.selection_window_first_item = 0
                    max_window_start = max(0, n_items - MAX_DISPLAY_ELEMENTS)
                    if(self.selection_window_first_item>max_window_start):
                        self.selection_window_first_item = max_window_start
                    #print("self.selection_window_first_item set at " + str(self.selection_window_first_item))


            
            #
            # HANDLE SCROLL
            #

            #
            # If the selection is above or below the selection y range, scroll it.
            # SCROLL_MAX_SPEED = 12
            # SCROLL_ZONE_SIZE = 0.3 # in normalized space, for how much the finger will trigger the scroll up/down

            # scroll_factor = 0.0
            # if(self.normalized_finger_y > 1.0 and self.normalized_finger_y < (1.0 + SCROLL_ZONE_SIZE)):
            #     scroll_factor = (self.normalized_finger_y - 1.0) / SCROLL_ZONE_SIZE
            # elif(self.normalized_finger_y < 0.0 and self.normalized_finger_y > (0.0 - SCROLL_ZONE_SIZE)):
            #     scroll_factor = (self.normalized_finger_y) / SCROLL_ZONE_SIZE


            SCROLL_MAX_SPEED = 2   # Items per second
            SCROLL_ZONE_SIZE = 30 # in mm, the height of the scroll which will be normalized from 0 to 1

            scroll_factor = 0.0

            # if(y>SELECTION_MAX_Y):
            #     scroll_factor = (y - SELECTION_MAX_Y) / SCROLL_ZONE_SIZE
            #     scroll_factor = min(scroll_factor, 1.0)
            #     # scroll factor normalized and clamped in [0-1].
            # elif(y<SELECTION_MIN_Y):
            #     scroll_factor = (y - SELECTION_MIN_Y) / SCROLL_ZONE_SIZE
            #     scroll_factor = max(scroll_factor, -1.0)
            #     # scroll factor normalized and clamped in [-1,0].


            if(offset_y>selection_stable_range):
                scroll_factor = (offset_y - selection_stable_range) / SCROLL_ZONE_SIZE
                scroll_factor = min(scroll_factor, 1.0)
                # scroll factor normalized and clamped in [0-1].
            elif(offset_y<selection_stable_range):
                scroll_factor = (offset_y + selection_stable_range) / SCROLL_ZONE_SIZE
                scroll_factor = max(scroll_factor, -1.0)
                # scroll factor normalized and clamped in [-1,0].


            #print("scroll="+str(scroll_factor))

            #boot the scrool
            scroll_factor = (scroll_factor * 1.6) ** 3
            # Note that the above formula KEEPS the sign!

            #print("scroll2="+str(scroll_factor))

            if(scroll_factor != 0.0):
                delta_scroll = - scroll_factor * SCROLL_MAX_SPEED * dt
                #print("Scrolling f "+str(scroll_factor) + " -> " + str(delta_scroll))
                self.selection_window_first_item += delta_scroll
            
                if(self.selection_window_first_item<0):
                    self.selection_window_first_item = 0
                max_window_start = max(0, n_items - MAX_DISPLAY_ELEMENTS)
                if(self.selection_window_first_item>max_window_start):
                    self.selection_window_first_item = max_window_start



            #
            # FIND SELECTED OBJECT AND APPLY POSE
            #

            # At this point:
            # self.selection_window_first_item is the (float) id of the first element to display in the list
            # self.normalized_finger_y is the 0-1 factor to decide which element to select in the available space
            
            #
            # Finally, if the finger is in the selection y range, calculate the selection number and apply the current pose
            clamped_y = max(min(self.normalized_finger_y, 1), 0.01)
            # We clamp between 0 < y <= 1 because this vale is later inverted to 0 =< y < 1 to select the element from the list.

            #first_id = int(self.selection_window_first_item)
            first_id = self.selection_window_first_item
            n_items_left = n_items - first_id
            n_items_left = min(n_items_left, MAX_DISPLAY_ELEMENTS)
            last_id = first_id + n_items_left

            # assert (last <= n_items) # yes, the last_id can be out of bounds, but we will never select it because the normalized selwction is clamped to 0 <= y < 1

            #self.selection_num = (int)((last_id-first_id) * (1-self.normalized_finger_y))
            self.selection_num = (last_id-first_id) * (1-clamped_y)
            self.selection_num += first_id
            selection_name = self.selectable_items[int(self.selection_num)]
            #print("Selected_item = " + str(self.selection_num))
            #print(str(self.selection_num) + "\t" + str(self.selection_window_first_item) + "\t" + str(last_id) + "\t" + str(clamped_y))

            applyPose(armature=self.selected_armature, pose_library_name=self.POSE_LIBRARY_NAME, hand_bone_names=self.HAND_BONE_NAMES, pose_name=selection_name, try_record=False)



        # Force interactive redraw
        if(bpy.context.area):
            bpy.context.area.tag_redraw()
        
        return {"RUNNING_MODAL"}
    

    def cancel(self, context):
        self.stop_leap_receiver()
        self.removeHandlers()        
        return {'CANCELLED'}


    def stop_leap_receiver(self):
        if(self.leap_receiver != None):
            print("Releasing LeapReceiver singleton...")
            LeapReceiver.releaseSingleton()
            #self.leap_receiver.terminate()
            #self.leap_receiver.join()
            #del(self.leap_receiver)
            self.leap_receiver = None
    

    FONT_MAX_SIZE = 48
    FONT_RGBA = (0.8, 0.8, 0.8, 0.9)
    SELECTED_FONT_RGBA = (0.8, 0.1, 0.2, 0.9)
    ICON_SIZE = 64
    BACKGROUND_COLOR = (0.15,0.1,0.1,0.9)

    def draw_callback_px(self, context):
        if(self.execution_active_space != None):
            if(not (self.execution_active_space.as_pointer() == context.area.spaces.active.as_pointer()) ):
                #print("Skipping...")
                return

        self.draw_callback_px_moving_text(context)
        #self.draw_callback_px_moving_arrow(context)


    #
    # MOVING TEXT
    #    

    def draw_callback_px_moving_text(self, context):

        DPI = bpy.context.user_preferences.system.dpi
        #print("Rendering moving text with DPI "+str(DPI))

        #print("selection= "+str(self.selection_num))
        int_selection_num = int(self.selection_num)


        n_items = len(self.selectable_items)

        # Phylosophy
        # We try to use the fraction of the current heght of the area, but up to a maximum font size

        # Number of items to really display
        n_items_to_display = min(MAX_DISPLAY_ELEMENTS, len(self.selectable_items))

        # Calculate the top point
        text_top_y = context.region.height * ( (1 + SELECTION_DISPLAY_HEIGHT) / 2)

        # calc the desired text area height
        desired_bottom_y = context.region.height * ( (1-SELECTION_DISPLAY_HEIGHT) / 2)
        desired_text_area_height = text_top_y - desired_bottom_y
        # calc the desired the font size
        font_size = int(desired_text_area_height / n_items_to_display)
        # But limit the font size to their maximum
        font_size = min(font_size, self.FONT_MAX_SIZE)
        # The effective size of the text area, according to the chosen font size
        text_area_height = font_size * n_items_to_display
        # recompute the bottom point according to chosen font size
        text_bottom_y = text_top_y - text_area_height

        #print(str(self.FONT_MAX_SIZE)+" fs="+str(font_size))


        central_y = text_bottom_y + int(text_area_height / 2)

        



        bgl.glPushAttrib(bgl.GL_CLIENT_ALL_ATTRIB_BITS)

        # Set the font size now, because it will be needed to estimate the background size.
        blf.size(0, font_size, DPI)

        
        #
        # Draw background
        max_text_width = 0
        for item in self.selectable_items:
            item_w,item_h = blf.dimensions(0, item)
            if(item_w > max_text_width):
                max_text_width = item_w
        self.draw_bg(context, top_y=text_top_y, bottom_y=text_bottom_y, width=max_text_width*1.5, cover_pointer=True)
        


        #
        # Draw entries
        #
        pos_x = 0
        
        # The first item will be drawn on the very top, according to the current selection and its position on screen
        # The offset uses (self.selection_num - 1) because the text is written with the y at the bottom line.
        # However, it is easier to calculate thinking of its beginning at the top. So we shift the text up of one line.
        pos_y =  central_y + ( (self.selection_num - 1) * font_size)


        for item_id in range(0,n_items):
            item = self.selectable_items[item_id]
            
            item_w,item_h = blf.dimensions(0, item)
            pos_x = (context.region.width / 2) - item_w
            
            blf.position(0, pos_x, pos_y, 0)
            
            if(item_id == int_selection_num):
                bgl.glColor4f(*self.SELECTED_FONT_RGBA)
            else:
                bgl.glColor4f(*self.FONT_RGBA)

            #print("Drawing item at "+str(pos_x) + "\t" + str(pos_y))
            blf.draw(0, item)

            pos_y -= font_size
                
            
        bgl.glPopAttrib()


        #
        # Draw pointing finger
        bgl.glPushClientAttrib(bgl.GL_CURRENT_BIT|bgl.GL_ENABLE_BIT)
        
        # transparence
        bgl.glEnable(bgl.GL_BLEND)
        bgl.glBlendFunc(bgl.GL_SRC_ALPHA, bgl.GL_ONE_MINUS_SRC_ALPHA)

        # The finger icon (64x64) has the finget tip at pixel 23 from the top, or 40 from the bottom
        pos_y = central_y - 40
        
        if(not self.selector_visible):
            pos_x = (context.region.width / 2) + self.ICON_SIZE
            bgl.glRasterPos2f(pos_x, pos_y)
            bgl.glDrawPixels(self.ICON_SIZE, self.ICON_SIZE, bgl.GL_RGBA, bgl.GL_FLOAT, icon_pointing_finger_missing)
        else:
            pos_x = (context.region.width / 2)
            bgl.glRasterPos2f(pos_x, pos_y)
            bgl.glDrawPixels(self.ICON_SIZE, self.ICON_SIZE, bgl.GL_RGBA, bgl.GL_FLOAT, icon_pointing_finger)

        bgl.glPopClientAttrib()

       
        pass



    #
    # MOVING ARROW
    #

    def draw_callback_px_moving_arrow(self, context):
        #print("drawing")

        DPI = bpy.context.user_preferences.system.dpi
        print("Rendering moving arrow with DPI "+str(DPI))


        int_selection_num = int(self.selection_num)
        n_items = len(self.selectable_items)

        

        # Phylosophy
        # We try to use the fraction of the current heght of the area, but up to a maximum font size

        # Number of items to really display
        n_items_to_display = min(MAX_DISPLAY_ELEMENTS, len(self.selectable_items))

        # Calculate the top point
        text_top_y = context.region.height * ( (1 + SELECTION_DISPLAY_HEIGHT) / 2)

        # calc the desired text area height
        desired_bottom_y = context.region.height * ( (1-SELECTION_DISPLAY_HEIGHT) / 2)
        desired_text_area_height = text_top_y - desired_bottom_y
        # calc the desired the font size
        font_size = int(desired_text_area_height / n_items_to_display)
        # But limit the font size to their maximum
        font_size = min(font_size, self.FONT_MAX_SIZE)
        # The effective size of the text area, according to the chosen font size
        text_area_height = font_size * n_items_to_display
        # recompute the bottom point according to chosen font size
        text_bottom_y = text_top_y - text_area_height

        #print("Font_size = " + str(font_size))
        
        
       
        bgl.glPushAttrib(bgl.GL_CLIENT_ALL_ATTRIB_BITS)

        blf.size(0, font_size, DPI)

        #
        # Draw background
        max_text_width = 0
        for item in self.selectable_items:
            item_w,item_h = blf.dimensions(0, item)
            if(item_w > max_text_width):
                max_text_width = item_w

        self.draw_bg(context=context, top_y=text_top_y, bottom_y=text_bottom_y, width=max_text_width*1.5)


        #
        # Draw entries
        pos_x = 0
        pos_y = text_top_y - font_size
        pos_y += self.selection_window_first_item * font_size
                
        for item_id in range(0,len(self.selectable_items)):
            item = self.selectable_items[item_id]
            
            item_w,item_h = blf.dimensions(0, item)
            pos_x = (context.region.width / 2) - item_w
            
            blf.position(0, pos_x, pos_y, 0)
            
            if(item_id == int_selection_num):
                bgl.glColor4f(*self.SELECTED_FONT_RGBA)
            else:
                bgl.glColor4f(*self.FONT_RGBA)

            blf.draw(0, item)            
            pos_y -= font_size

            
        bgl.glPopAttrib()
                
        #
        # Draw pointing finger
        bgl.glPushClientAttrib(bgl.GL_CURRENT_BIT|bgl.GL_ENABLE_BIT)
        
        # transparence
        bgl.glEnable(bgl.GL_BLEND)
        bgl.glBlendFunc(bgl.GL_SRC_ALPHA, bgl.GL_ONE_MINUS_SRC_ALPHA)
        
        if(not self.selector_visible):
            pos_x = (context.region.width / 2) + self.ICON_SIZE
            pos_y = text_top_y - self.ICON_SIZE
            bgl.glRasterPos2f(pos_x, pos_y)
            bgl.glDrawPixels(self.ICON_SIZE, self.ICON_SIZE, bgl.GL_RGBA, bgl.GL_FLOAT, icon_pointing_finger_missing)
        else:
            pos_x = (context.region.width / 2)
            pos_y = text_bottom_y + self.normalized_finger_y * text_area_height - (self.ICON_SIZE * 0.625)
            bgl.glRasterPos2f(pos_x, pos_y)
            bgl.glDrawPixels(self.ICON_SIZE, self.ICON_SIZE, bgl.GL_RGBA, bgl.GL_FLOAT, icon_pointing_finger)

        bgl.glPopClientAttrib()

        pass

    
    pass



    def draw_bg(self, context, top_y, bottom_y, width, cover_pointer=False):
        bgl.glEnable(bgl.GL_BLEND)
        bgl.glBlendFunc(bgl.GL_SRC_ALPHA, bgl.GL_ONE_MINUS_SRC_ALPHA)

        bgl.glBegin(bgl.GL_QUADS)
        
        right_x = context.region.width / 2
        left_x = right_x - width

        if(cover_pointer):
            right_x += self.ICON_SIZE
                
        bgl.glVertex2f(left_x,bottom_y)
        bgl.glColor4f(*self.BACKGROUND_COLOR)
        bgl.glVertex2f(right_x, bottom_y)
        bgl.glVertex2f(right_x, top_y)
        bgl.glVertex2f(left_x, top_y)
        
        bgl.glEnd()



def applyPose(armature, pose_library_name, hand_bone_names, pose_name, try_record):
    #pose_name = bpy.data.actions[pose_library_name].pose_markers[pose_number].name
    #print("Applying pose " +pose_name)
    poses_data = getPoseLibraryData(pose_library_name, hand_bone_names)
    bones_data = poses_data[pose_name]
    applyBoneRotations(armature, bones_data, try_record)



def applyBoneRotations(armature, rotations, try_record):
    """Takes as input the reference to the armature, and a dictionary with keys=bone_names, and values a 4-element list with the quaternion values [w x y z]."""

    bones = armature.pose.bones
    for bone_name in rotations:
        #print("Applying " + bone_name)
        # e.g. bpy.data.objects['Human1-mhxrig-expr-advspine'].pose.bones['Finger-2-1_L'].rotation_quaternion = 1,0,0,0
        bones[bone_name].rotation_quaternion = rotations[bone_name]

        #print("Checking rec for "+bone_name)
        # RECORD (eventually)
        if(try_record and bpy.context.scene.tool_settings.use_keyframe_insert_auto):
            print("Recording keyframe for "+bone_name)
            frame = bpy.context.scene.frame_current
            bones[bone_name].keyframe_insert(data_path="rotation_quaternion", frame=frame)


# def retrieveFingerControllerRotations(armature, controller_names):
#     """Returns a list of rotations. In the order procided by the controller names."""
#     out = []

#     bones = armature.pose.bones
#     for cname in controller_names:
#         controller = bones[cname]
#         out.append(mathutils.Quaternion(controller.rotation_quaternion))

#     return out


def resetFingerControllers(armature, controller_names, try_record):

    bones = armature.pose.bones
    for cname in controller_names:
        controller = bones[cname]
        controller.rotation_quaternion = 1,0,0,0

        if(try_record and bpy.context.scene.tool_settings.use_keyframe_insert_auto):
            frame = bpy.context.scene.frame_current
            controller.keyframe_insert(data_path="rotation_quaternion", frame=frame)



def retrieveBoneRotations(armature, bone_names):
    """Takes as input the reference to the armature and the list of names of the bones to retrieve.
    Returns a dictionary with keys=bone_names, and values a 4-element list with the quaternion values [w x y z]."""

    out = {}
    
    bones = armature.pose.bones
    for bone in bones:
        bone_name = bone.name
        #print("Applying " + bone_name)
        # e.g. bpy.data.objects['Human1-mhxrig-expr-advspine'].pose.bones['Finger-2-1_L'].rotation_quaternion = 1,0,0,0
        w,x,y,z = bone.rotation_quaternion
        out[bone_name] = [w,x,y,z]
    
    return out




def getPoseLibraryData(pose_library_name, bones):
    """Returns a dictionary. Keys are the pose names. Values are the pose data.
    Each value will be dictionary with bone names as keys, and the a list of the 4 rotation elements as value: "bone_name" -> [w x y z]
    Only the bones specified in the bones parameter will be considered.
    For bones specified in the list but missing in the action data, rotation will be defaulted to identity [1 0 0 0].
    """
    
    # Prepare the output dictionary.
    # key=action_name, data=dict of rotations
    out = {}
    
    library_action = bpy.data.actions[pose_library_name]
    
    # Everything at identity by default
    for marker in library_action.pose_markers:
        pose_name = marker.name
        frame_number = marker.frame
        #print("--> " + pose_name + " @ " + str(frame_number))
        #prepare the dict of rotations.
        # key = bone_name, data=quaternion_elements
        dict_of_rotations = {}
        for bone in bones:
            dict_of_rotations[bone] = [ 1, 0, 0, 0 ]
            
        # Insert the bone->rotation dictionary into the main output dict
        out[pose_name] = dict_of_rotations

    # e.g. pose.bones["Head"].rotation_quaternion
    pattern = re.compile('pose\.bones\[\"(.+)\"\]\.rotation_quaternion') #  "pose\.bones\[\"(+*)\"\]\..+")

        
    # Now really parse the data
    for fcurve in library_action.fcurves:
        res = pattern.match(fcurve.data_path)
        if(res == None):
            continue

        bone_name = res.group(1)
        if(bone_name in bones):
            for kf in fcurve.keyframe_points:
            #kf = fcurve.keyframe_points[frame_number]
                t,val = kf.co
                #print("t="+str(t))
                # In a library, poses are indexed form 0, but keyframes start form 1
                pose_number = int(t) -1
                
                assert pose_number < len(library_action.pose_markers)
                marker = library_action.pose_markers[pose_number]
                pose_name = marker.name
                assert(pose_name in out)
                dict_of_rotations = out[pose_name]
                
                #print("Inserting for pose "+pose_name+"\tbone "+bone_name+"\trot_element " + str(fcurve.array_index))
                # the data_index will be between 0 and 3, indicating the quaternion component wxyz
                dict_of_rotations[bone_name][fcurve.array_index] = val
    
        
    return out




def loadImageEventually(image_file):
    """Load the image from the 'images' directory relative to the scene.
        If the image is already loaded just return it.
    """

    # list the names of already loaded images
    loaded_images_files = [img.name for img in bpy.data.images]
    
    scene_dir = os.path.dirname(bpy.data.filepath)
    
    if(not image_file in loaded_images_files):
        print("Loading image from '" + image_file + "'")
        image = image_utils.load_image(imagepath=image_file, dirname=scene_dir+"/images/")
    else:
        print("Image '" + image_file + "' already loaded. Skipping...")
        image = bpy.data.images[image_file]
    return image



#
# This database is used when filtering the handshapes using finger extension
# Each handshape (which might appear in the hands_library) is followed by a bitmask specifying which finger are extended, plus a flag for the "lasso"
# key = shapename
# data = (6 bit flags) <lasso> <pinky> <ring> <medium> <index> <thumb>
# e.g. 'a': 0b000000 ; 'l': 0b000011; 'o': 0b100000, ...

# For each pose in the animation library, mark which finger MUST BE OPEN for the sign to be valid
finger_open_db = {
#    'ClosedHand':   0b00000,
    'OpenHand':     0b11111,
#    'a':    0b00000,
    'b':    0b11110,
#    'c':    0b00000,
    'd':    0b00010,
#    'e':    0b00000,
    'f':    0b11100,
    'g':    0b00010,
    'h':    0b00110,
    'i':    0b10000,
    'j':    0b10000,
    'k':    0b00111,
    'l':    0b00011,
    'm':    0b01110,
    'n':    0b00110,
#    'o':    0b00000,
    'p':    0b00111,
    'q':    0b00011,
    'r':    0b00110,
#    's':    
    'sch':  0b11111,
    't':    0b00010,
    'u':    0b00110,
    'v':    0b00110,
    'w':    0b01110,
#    'x':
    'y':    0b10001,
    'z':    0b00010
}

# For each pose in the animation library, mark which finger MUST BE CLOSED for the sign to be valid
finger_closed_db = {
    'ClosedHand':   0b11111,
#    'OpenHand':     0b11111,
    'a':    0b11110,
    'b':    0b00001,
#    'c':    0b00000,
#    'd':    0b00010,
    'e':    0b11111,
    'f':    0b00010,
    'g':    0b11100,
    'h':    0b11000,
    'i':    0b01111,
    'j':    0b01111,
    'k':    0b11000,
    'l':    0b11100,
    'm':    0b10000,
    'n':    0b110000,
#    'o':    0b00000,
    'p':    0b11000,
    'q':    0b11100,
    'r':    0b110011,
    's':    0b11111,
#    'sch':  0b11111,
    't':    0b11100,
    'u':    0b11000,
    'v':    0b11000,
    'w':    0b10000,
    'x':    0b11101,
    'y':    0b01110,
    'z':    0b11100    
}

# These poses are selectable only if a pinch is detected in the hand
pinch_needed_set = ('d','f','o')


# returns the bitflag as needed by the finger extension dictionary.
def getHandBitFlag(hand_id, leap_dict):
    out = 0

    pointables = leap_dict['pointables']
    # Scan each pointable
    for p in pointables:
        # If it pertains to this hand
        if(p['handId'] == hand_id):
            # if it is extended
            if( p['extended'] ):
                # merge the flag
                # the 'type' is the ordered finger number (0=thumb, 1=index, ...)
                out |= 1 << p['type'] 

    # check for lasso
    for h in leap_dict['hands']:
        # if it is the correct hand
        if(h['id'] == hand_id):
            pinch_str = h['pinchStrength']
            # we decide a threshold
            if(pinch_str > 0.95):
                out |= 0b100000


    return out
    #return 0b101101





# I've got here the possible values for the 'name' parameter for the KeyMap
# https://svn.blender.org/svnroot/bf-extensions/contrib/py/scripts/addons/presets/keyconfig/blender_2012_experimental.py
#EDIT_MODES = ['Object Mode', 'Pose']
EDIT_MODES = ['Pose']

# store keymaps here to access after registration
hand_selection_keymap_items = []

icon_pointing_finger = None
icon_pointing_finger_missing = None


def register():
    global icon_pointing_finger
    global icon_pointing_finger_missing

    # Register properties
    bpy.types.WindowManager.leap_hand_shape_selector_finger_extension_filter = bpy.props.BoolProperty(name="Finger Extension Filter", description="Extending or retracting the fingers filter out the number of available letters to show in the hand dhape selector.", default=False, options={'SKIP_SAVE'})

    # Register classes
    bpy.utils.register_class(HandShapeSelector)
    
    #
    # LOAD ICONS
    image = loadImageEventually(image_file="1-finger-point-left-icon-red.png")
    icon_pointing_finger = bgl.Buffer(bgl.GL_FLOAT, len(image.pixels), image.pixels)
    image = loadImageEventually(image_file="1-finger-point-left-icon-red-missing.png")
    icon_pointing_finger_missing = bgl.Buffer(bgl.GL_FLOAT, len(image.pixels), image.pixels)
    
    
    # handle the keymap
    wm = bpy.context.window_manager
    
    km = wm.keyconfigs.addon.keymaps.new(name='Pose', space_type='EMPTY')
    kmi = km.keymap_items.new(HandShapeSelector.bl_idname, RHAND_ACTIVATION_CHAR, 'PRESS', ctrl=True, shift=True)
    kmi.properties.use_right_hand = True
    hand_selection_keymap_items.append((km, kmi))

    kmi = km.keymap_items.new(HandShapeSelector.bl_idname, LHAND_ACTIVATION_CHAR, 'PRESS', ctrl=True, shift=True)
    kmi.properties.use_right_hand = False
    hand_selection_keymap_items.append((km, kmi))
 
    pass

def unregister():
    global icon_pointing_finger
    global icon_pointing_finger_missing

    # handle the keymap
    for km, kmi in hand_selection_keymap_items:
        km.hand_selection_keymap_items.remove(kmi)
    hand_selection_keymap_items.clear()

    print("ok")

    icon_pointing_finger = None
    icon_pointing_finger_missing = None

    # Unregister the class
    bpy.utils.unregister_class(HandShapeSelector)

    # Unregister properties
    del bpy.context.window_manager.leap_hand_shape_selector_finger_extension_filter

    pass

if __name__ == "__main__":
    register()

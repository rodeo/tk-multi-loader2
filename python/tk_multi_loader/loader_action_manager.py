# Copyright (c) 2015 Shotgun Software Inc.
# 
# CONFIDENTIAL AND PROPRIETARY
# 
# This work is provided "AS IS" and subject to the Shotgun Pipeline Toolkit 
# Source Code License included in this distribution package. See LICENSE.
# By accessing, using, copying or modifying this work you indicate your 
# agreement to the Shotgun Pipeline Toolkit Source Code License. All rights 
# not expressly granted therein are reserved by Shotgun Software Inc.

import sgtk
import hashlib
import datetime
import os
import copy
import sys
from sgtk.platform.qt import QtCore, QtGui
from tank_vendor import shotgun_api3
from sgtk import TankError

from .action_manager import ActionManager

class LoaderActionManager(ActionManager):
    """
    Specialisation of the base ActionManager class that handles dishing out and 
    executing QActions based on the hook configuration for the regular loader UI
    """

    def __init__(self):
        """
        Constructor
        """
        ActionManager.__init__(self)

        self._app = sgtk.platform.current_bundle()
        
        # are we old school or new school with publishes?
        publish_entity_type = sgtk.util.get_published_file_entity_type(self._app.sgtk)
        
        if publish_entity_type == "PublishedFile":
            self._publish_type_field = "published_file_type"
        else:
            self._publish_type_field = "tank_type"
        
    
    def _get_named_actions_for_publish(self, sg_data, ui_area):
        """
        Retrieves the dictionary of actions for a given publish.

        :param sg_data: Publish to retrieve actions for
        :param ui_area: Indicates which part of the UI the request is coming from.
                        Currently one of UI_AREA_MAIN, UI_AREA_DETAILS and UI_AREA_HISTORY
        :return: Dictionary of actions indexed by their name.
        """

        # Figure out the type of the publish
        publish_type_dict = sg_data.get(self._publish_type_field)
        if publish_type_dict is None:
            # this publish does not have a type
            publish_type = "undefined"
        else:
            publish_type = publish_type_dict["name"]
        
        # check if we have logic configured to handle this publish type.
        mappings = self._app.get_setting("action_mappings")
        # returns a structure on the form
        # { "Maya Scene": ["reference", "import"] }
        actions = mappings.get(publish_type, [])
        
        if len(actions) == 0:
            return []
        
        # cool so we have one or more actions for this publish type.
        # resolve UI area
        if ui_area == LoaderActionManager.UI_AREA_DETAILS:
            ui_area_str = "details"
        elif ui_area == LoaderActionManager.UI_AREA_HISTORY:
            ui_area_str = "history"
        elif ui_area == LoaderActionManager.UI_AREA_MAIN:
            ui_area_str = "main"
        else:
            raise TankError("Unsupported UI_AREA. Contact support.")

        # convert created_at unix time stamp to shotgun time stamp
        unix_timestamp = sg_data.get("created_at")
        if isinstance(unix_timestamp, float):
            sg_timestamp = datetime.datetime.fromtimestamp(unix_timestamp, 
                                                           shotgun_api3.sg_timezone.LocalTimezone())
            sg_data["created_at"] = sg_timestamp

        action_defs_dict = {}
        try:
            # call out to hook to give us the specifics.
            action_defs_list = self._app.execute_hook_method("actions_hook",
                                                       "generate_actions",
                                                       sg_publish_data=sg_data,
                                                       actions=actions,
                                                       ui_area=ui_area_str)
            for action_def in action_defs_list:
                action_defs_dict[action_def["name"]] = action_def

        except Exception:
            self._app.log_exception("Could not execute generate_actions hook.")

        return action_defs_dict

    def get_actions_for_publishes(self, sg_data_list, ui_area):
        """
        Returns a list of actions for a publish.

        Shotgun data representing a publish is passed in and forwarded on to hooks
        to help them determine which actions may be applicable. This data should by convention
        contain at least the following fields:

          "published_file_type",
          "tank_type"
          "name",
          "version_number",
          "image",
          "entity",
          "path",
          "description",
          "task",
          "task.Task.sg_status_list",
          "task.Task.due_date",
          "task.Task.content",
          "created_by",
          "created_at",                     # note: as a unix time stamp
          "version",                        # note: not supported on TankPublishedFile so always None
          "version.Version.sg_status_list", # (also always none for TankPublishedFile)
          "created_by.HumanUser.image"

        This ensures consistency for any hooks implemented by users.

        :param sg_data_list: Shotgun data list of the publishes
        :param ui_area: Indicates which part of the UI the request is coming from.
                        Currently one of UI_AREA_MAIN, UI_AREA_DETAILS and UI_AREA_HISTORY
        :returns: List of QAction objects, ready to be parented to some QT Widgetry.
        """
        # If the selection is empty, there's no actions to return.
        if len(sg_data_list) == 0:
            return []

        # We are going to do an intersection of all the entities' actions. We'll pick the actions from
        # the first item to initialize the intersection...
        first_entity_actions = self._get_named_actions_for_publish(sg_data_list[0], ui_area)

        intersection_actions_per_name = dict(
            [(action_name, [(sg_data_list[0], action)]) for action_name, action in first_entity_actions.iteritems()]
        )

        # ... and then we'll remove actions from that set as we encounter entities without those actions.

        # We've already processed the first entry, no need to intersect with itself.
        sg_data_list = sg_data_list[1:]

        # So, for each action in the initial intersection...
        #
        # Get a copy of the keys because we're about to remove keys
        # as they are visited if an action is not common to every action.
        for name in intersection_actions_per_name.keys():

            # Check if the other entities have the same actions
            for sg_data in sg_data_list:
                entity_actions = self._get_named_actions_for_publish(
                   sg_data, self.UI_AREA_DETAILS
                )
                # If the current action is part of this entity's actions, then track that
                # entity's action parameters.
                if name in entity_actions:
                    intersection_actions_per_name[name].append((sg_data, action))
                else:
                    # Otherwise remove this action from the intersection
                    del intersection_actions_per_name[name]
                    # No need to look for the remaining entities, this action is out of the intersection.
                    break

        # For every actions in the intersection, create an associated QAction with appropriate callback
        # and hook parameters.
        actions = []
        for action_list in intersection_actions_per_name.values():

            # We need to title the action, so pick the caption and description of the first item.
            _, first_action_def = action_list[0]
            name = first_action_def["name"]
            caption = first_action_def["caption"]
            description = first_action_def["description"]

            a = QtGui.QAction(caption, None)
            a.setToolTip(description)

            # Create a generator that will return every (publish info, hook param) pairs for invoking
            # the hook.
            pairs = ((sg_data, action_def["params"]) for (sg_data, action_def) in action_list)

            # Bind all the pairs to a single invocation of the _execute_hook.
            a.triggered[()].connect(
                lambda n=name, data_list=pairs: self._execute_hook(n, data_list)
            )
            actions.append(a)

        return actions

    def get_actions_for_publish(self, sg_data, ui_area):
        """
        See documentation for get_actions_for_publish. The functionality is the same, but only for
        a single publish.
        """
        return self.get_actions_for_publishes([sg_data], ui_area)

    def get_default_action_for_publish(self, sg_data, ui_area):
        """
        Get the default action for the specified publish data.
        
        The default action is defined as the one that appears first in the list in the 
        action mappings.

        :param sg_data: Shotgun data for a publish
        :param ui_area: Indicates which part of the UI the request is coming from. 
                        Currently one of UI_AREA_MAIN, UI_AREA_DETAILS and UI_AREA_HISTORY
        :returns:       The QAction object representing the default action for this publish
        """
        # this could probably be optimised but for now get all actions:
        actions = self.get_actions_for_publish(sg_data, ui_area)
        # and return the first one:
        return actions[0] if actions else None

    def has_actions(self, publish_type):
        """
        Returns true if the given publish type has any actions associated with it.
        
        :param publish_type: A Shotgun publish type (e.g. 'Maya Render')
        :returns: True if the current actions setup knows how to handle this.
        """
        mappings = self._app.get_setting("action_mappings")

        # returns a structure on the form
        # { "Maya Scene": ["reference", "import"] }
        my_mappings = mappings.get(publish_type, [])
        
        return len(my_mappings) > 0
        
    def get_actions_for_folder(self, sg_data):
        """
        Returns a list of actions for a folder object.
        """
        fs = QtGui.QAction("Show in the file system", None)
        fs.triggered[()].connect(lambda f=sg_data: self._show_in_fs(f))
        
        sg = QtGui.QAction("Show details in Shotgun", None)
        sg.triggered[()].connect(lambda f=sg_data: self._show_in_sg(f))

        sr = QtGui.QAction("Show in Screening Room", None)
        sr.triggered[()].connect(lambda f=sg_data: self._show_in_sr(f))
        
        return [fs, sg, sr]
    
    ########################################################################################
    # callbacks
    
    def _execute_hook(self, action_name, data_pairs):
        """
        callback - executes a hook
        """
        self._app.log_debug("Calling scene load hook.")
        
        try:
            for sg_data, params in data_pairs:
                self._app.execute_hook_method("actions_hook",
                                              "execute_action",
                                              name=action_name,
                                              params=params,
                                              sg_publish_data=sg_data)
        except Exception, e:
            self._app.log_exception("Could not execute execute_action hook.")
            QtGui.QMessageBox.critical(None, "Hook Error", "Error: %s" % e)
    
    def _show_in_sg(self, entity):
        """
        Callback - Shows a shotgun entity in the web browser
        
        :param entity: std sg entity dict with keys type, id and name
        """
        url = "%s/detail/%s/%d" % (self._app.sgtk.shotgun.base_url, entity["type"], entity["id"])                    
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))

    def _show_in_sr(self, entity):
        """
        Callback - Shows a shotgun entity in screening room
        
        :param entity: std sg entity dict with keys type, id and name
        """
        url = "%s/page/screening_room?entity_type=%s&entity_id=%d" % (self._app.sgtk.shotgun.base_url, 
                                                                      entity["type"], 
                                                                      entity["id"])                    
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))
    
    def _show_in_fs(self, entity):
        """
        Callback - Shows a shotgun entity in the file system
        
        :param entity: std sg entity dict with keys type, id and name
        """
        paths = self._app.sgtk.paths_from_entity(entity["type"], entity["id"])    
        for disk_location in paths:
                
            # get the setting        
            system = sys.platform
            
            # run the app
            if system == "linux2":
                cmd = 'xdg-open "%s"' % disk_location
            elif system == "darwin":
                cmd = 'open "%s"' % disk_location
            elif system == "win32":
                cmd = 'cmd.exe /C start "Folder" "%s"' % disk_location
            else:
                raise Exception("Platform '%s' is not supported." % system)
            
            exit_code = os.system(cmd)
            if exit_code != 0:
                self._engine.log_error("Failed to launch '%s'!" % cmd)
    

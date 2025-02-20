"""
This module contains the code for the abstract base collection that powers all the other collections.
"""

# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright 2006-2009, Red Hat, Inc and Others
# SPDX-FileCopyrightText: Michael DeHaan <michael.dehaan AT gmail>

import logging
import time
import os
import uuid
from threading import Lock
from typing import List, Union

from cobbler import utils
from cobbler.items import (
    package,
    system,
    item as item_base,
    image,
    profile,
    repo,
    mgmtclass,
    distro,
    file,
    menu,
)

from cobbler.cexceptions import CX


class Collection:
    """
    Base class for any serializable list of things.
    """

    def __init__(self, collection_mgr):
        """
        Constructor.

        :param collection_mgr: The collection manager to resolve all information with.
        """
        self.collection_mgr = collection_mgr
        self.listing = {}
        self.api = self.collection_mgr.api
        self.lite_sync = None
        self.lock = Lock()
        self.logger = logging.getLogger()

    def __iter__(self):
        """
        Iterator for the collection. Allows list comprehensions, etc.
        """
        for obj in list(self.listing.values()):
            yield obj

    def __len__(self):
        """
        Returns size of the collection.
        """
        return len(list(self.listing.values()))

    def factory_produce(self, api, seed_data):
        """
        Must override in subclass. Factory_produce returns an Item object from dict.

        :param api: The API to resolve all information with.
        :param seed_data: Unused Parameter in the base collection.
        """
        raise NotImplementedError()

    def remove(
        self,
        name: str,
        with_delete: bool = True,
        with_sync: bool = True,
        with_triggers: bool = True,
        recursive: bool = False,
    ):
        """
        Remove an item from collection. This method must be overridden in any subclass.

        :param name: Item Name
        :param with_delete: sync and run triggers
        :param with_sync: sync to server file system
        :param with_triggers: run "on delete" triggers
        :param recursive: recursively delete children
        :returns: NotImplementedError
        """
        raise NotImplementedError(
            "Please implement this in a child class of this class."
        )

    def get(self, name: str):
        """
        Return object with name in the collection

        :param name: The name of the object to retrieve from the collection.
        :return: The object if it exists. Otherwise None.
        """
        return self.listing.get(name.lower(), None)

    def find(
        self, name: str = "", return_list: bool = False, no_errors=False, **kargs: dict
    ) -> Union[List[item_base.Item], item_base.Item, None]:
        """
        Return first object in the collection that matches all item='value' pairs passed, else return None if no objects
        can be found. When return_list is set, can also return a list.  Empty list would be returned instead of None in
        that case.

        :param name: The object name which should be found.
        :param return_list: If a list should be returned or the first match.
        :param no_errors: If errors which are possibly thrown while searching should be ignored or not.
        :param kargs: If name is present, this is optional, otherwise this dict needs to have at least a key with
                      ``name``. You may specify more keys to finetune the search.
        :return: The first item or a list with all matches.
        :raises ValueError: In case no arguments for searching were specified.
        """
        matches = []

        if name:
            kargs["name"] = name

        kargs = self.__rekey(kargs)

        # no arguments is an error, so we don't return a false match
        if len(kargs) == 0:
            raise ValueError("calling find with no arguments")

        # performance: if the only key is name we can skip the whole loop
        if len(kargs) == 1 and "name" in kargs and not return_list:
            try:
                return self.listing.get(kargs["name"].lower(), None)
            except Exception:
                return self.listing.get(kargs["name"], None)

        with self.lock:
            for (_, obj) in list(self.listing.items()):
                if obj.find_match(kargs, no_errors=no_errors):
                    matches.append(obj)

        if not return_list:
            if len(matches) == 0:
                return None
            return matches[0]
        return matches

    SEARCH_REKEY = {
        "kopts": "kernel_options",
        "kopts_post": "kernel_options_post",
        "inherit": "parent",
        "ip": "ip_address",
        "mac": "mac_address",
        "virt-auto-boot": "virt_auto_boot",
        "virt-file-size": "virt_file_size",
        "virt-disk-driver": "virt_disk_driver",
        "virt-ram": "virt_ram",
        "virt-path": "virt_path",
        "virt-type": "virt_type",
        "virt-bridge": "virt_bridge",
        "virt-cpus": "virt_cpus",
        "virt-host": "virt_host",
        "virt-group": "virt_group",
        "dhcp-tag": "dhcp_tag",
        "netboot-enabled": "netboot_enabled",
        "enable_gpxe": "enable_ipxe",
        "boot_loader": "boot_loaders",
    }

    def __rekey(self, _dict: dict) -> dict:
        """
        Find calls from the command line ("cobbler system find") don't always match with the keys from the datastructs
        and this makes them both line up without breaking compatibility with either. Thankfully we don't have a LOT to
        remap.

        :param _dict: The dict which should be remapped.
        :return: The dict which can now be understood by the cli.
        """
        new_dict = {}
        for key in list(_dict.keys()):
            if key in self.SEARCH_REKEY:
                newkey = self.SEARCH_REKEY[key]
                new_dict[newkey] = _dict[key]
            else:
                new_dict[key] = _dict[key]
        return new_dict

    def to_list(self) -> list:
        """
        Serialize the collection

        :return: All elements of the collection as a list.
        """
        return [x.to_dict() for x in list(self.listing.values())]

    def from_list(self, _list: list):
        """
        Create all collection object items from ``_list``.

        :param _list: The list with all item dictionaries.
        """
        if _list is None:
            return
        for item_dict in _list:
            item = self.factory_produce(self.api, item_dict)
            self.add(item)

    def copy(self, ref, newname):
        """
        Copy an object with a new name into the same collection.

        :param ref: The reference to the object which should be copied.
        :param newname: The new name for the copied object.
        """
        ref = ref.make_clone()
        ref.uid = uuid.uuid4().hex
        ref.ctime = time.time()
        ref.name = newname
        if ref.COLLECTION_TYPE == "system":
            # this should only happen for systems
            for interface in ref.interfaces:
                # clear all these out to avoid DHCP/DNS conflicts
                ref.interfaces[interface].dns_name = ""
                ref.interfaces[interface].mac_address = ""
                ref.interfaces[interface].ip_address = ""

        self.add(
            ref,
            save=True,
            with_copy=True,
            with_triggers=True,
            with_sync=True,
            check_for_duplicate_names=True,
        )

    def rename(
        self,
        ref: item_base.Item,
        newname,
        with_sync: bool = True,
        with_triggers: bool = True,
    ):
        """
        Allows an object "ref" to be given a new name without affecting the rest of the object tree.

        :param ref: The reference to the object which should be renamed.
        :param newname: The new name for the object.
        :param with_sync: If a sync should be triggered when the object is renamed.
        :param with_triggers: If triggers should be run when the object is renamed.
        """
        # Nothing to do when it is the same name
        if newname == ref.name:
            return

        # Save the old name
        oldname = ref.name
        # Reserve the new name
        self.listing[newname] = None
        # Delete the old item
        self.collection_mgr.serialize_delete_one_item(ref)
        self.listing.pop(oldname)
        # Change the name of the object
        ref.name = newname
        # Save just this item
        self.collection_mgr.serialize_one_item(ref)
        self.listing[newname] = ref

        # for mgmt classes, update all objects that use it
        if ref.COLLECTION_TYPE == "mgmtclass":
            for what in ["distro", "profile", "system"]:
                items = self.api.find_items(what, {"mgmt_classes": oldname})
                for item in items:
                    for i in range(0, len(item.mgmt_classes)):
                        if item.mgmt_classes[i] == oldname:
                            item.mgmt_classes[i] = newname
                    self.api.add_item(what, item, save=True)

        # for menus, update all objects that use it
        if ref.COLLECTION_TYPE == "menu":
            for what in ["profile", "image"]:
                items = self.api.find_items(what, {"menu": oldname})
                for item in items:
                    item.menu = newname
                    self.api.add_item(what, item, save=True)

        # for a repo, rename the mirror directory
        if ref.COLLECTION_TYPE == "repo":
            path = os.path.join(self.api.settings().webdir, "repo_mirror")
            old_path = os.path.join(path, oldname)
            if os.path.exists(old_path):
                new_path = os.path.join(path, ref.name)
                os.renames(old_path, new_path)

        # for a distro, rename the mirror and references to it
        if ref.COLLECTION_TYPE == "distro":
            path = ref.find_distro_path()

            # create a symlink for the new distro name
            ref.link_distro()

            # Test to see if the distro path is based directly on the name of the distro. If it is, things need to
            # updated accordingly.
            if os.path.exists(path) and path == str(
                os.path.join(self.api.settings().webdir, "distro_mirror", ref.name)
            ):
                newpath = os.path.join(
                    self.api.settings().webdir, "distro_mirror", ref.name
                )
                os.renames(path, newpath)

                # update any reference to this path ...
                distros = self.api.distros()
                for distro_obj in distros:
                    if distro_obj.kernel.find(path) == 0:
                        distro_obj.kernel = distro_obj.kernel.replace(path, newpath)
                        distro_obj.initrd = distro_obj.initrd.replace(path, newpath)
                        self.collection_mgr.serialize_one_item(distro_obj)

        if ref.COLLECTION_TYPE in ("profile", "system"):
            if ref.parent is not None:
                ref.parent.children.remove(oldname)

        # Now descend to any direct ancestors and point them at the new object allowing the original object to be
        # removed without orphanage. Direct ancestors will either be profiles or systems. Note that we do have to
        # care as setting the parent is only really meaningful for subprofiles. We ideally want a more generic parent
        # setter.
        kids = ref.get_children()
        for k in kids:
            if self.api.find_profile(name=k) is not None:
                k = self.api.find_profile(name=k)
                if ref.COLLECTION_TYPE == "distro":
                    k.distro = newname
                else:
                    k.parent = newname
                self.api.profiles().add(
                    k, save=True, with_sync=with_sync, with_triggers=with_triggers
                )
            elif self.api.find_menu(name=k) is not None:
                k = self.api.find_menu(name=k)
                k.parent = newname
                self.api.menus().add(
                    k, save=True, with_sync=with_sync, with_triggers=with_triggers
                )
            elif self.api.find_system(name=k) is not None:
                k = self.api.find_system(name=k)
                k.profile = newname
                self.api.systems().add(
                    k, save=True, with_sync=with_sync, with_triggers=with_triggers
                )
            else:
                raise CX(f'Internal error, unknown child type for child "{k}"!')

    def add(
        self,
        ref,
        save: bool = False,
        with_copy: bool = False,
        with_triggers: bool = True,
        with_sync: bool = True,
        quick_pxe_update: bool = False,
        check_for_duplicate_names: bool = False,
    ):
        """
        Add an object to the collection

        :param ref: The reference to the object.
        :param save: If this is true then the objet is persisted on the disk.
        :param with_copy: Is a bit of a misnomer, but lots of internal add operations can run with "with_copy" as False.
                          True means a real final commit, as if entered from the command line (or basically, by a user).
                          With with_copy as False, the particular add call might just be being run during
                          deserialization, in which case extra semantics around the add don't really apply. So, in that
                          case, don't run any triggers and don't deal with any actual files.
        :param with_sync: If a sync should be triggered when the object is renamed.
        :param with_triggers: If triggers should be run when the object is renamed.
        :param quick_pxe_update: This decides if there should be run a quick or full update after the add was done.
        :param check_for_duplicate_names: If the name of an object should be unique or not.
        :raises TypError: Raised in case ``ref`` is None.
        :raises ValueError: Raised in case the name of ``ref`` is empty.
        """
        if ref is None:
            raise TypeError("Unable to add a None object")

        ref.check_if_valid()

        if ref.uid == "":
            ref.uid = uuid.uuid4().hex

        if save:
            now = float(time.time())
            if ref.ctime == 0.0:
                ref.ctime = now
            ref.mtime = now

        if self.lite_sync is None:
            self.lite_sync = self.api.get_sync()

        # migration path for old API parameter that I've renamed.
        if with_copy and not save:
            save = with_copy

        if not save:
            # For people that aren't quite aware of the API if not saving the object, you can't run these features.
            with_triggers = False
            with_sync = False

        # Avoid adding objects to the collection with the same name
        if check_for_duplicate_names:
            for item in self.listing.values():
                if item.name == ref.name:
                    raise CX("An object already exists with that name. Try 'edit'?")

        if ref.COLLECTION_TYPE != self.collection_type():
            raise TypeError("API error: storing wrong data type in collection")

        # failure of a pre trigger will prevent the object from being added
        if save and with_triggers:
            utils.run_triggers(
                self.api,
                ref,
                f"/var/lib/cobbler/triggers/add/{self.collection_type()}/pre/*",
            )

        with self.lock:
            self.listing[ref.name.lower()] = ref

        # update children cache in parent object in case it is not in there already
        if ref.parent and ref.name not in ref.parent.children:
            ref.parent.children.append(ref.name)
            self.logger.debug(
                'Added child "%s" to parent "%s"', ref.name, ref.parent.name
            )

        # perform filesystem operations
        if save:
            # Save just this item if possible, if not, save the whole collection
            self.collection_mgr.serialize_one_item(ref)
            if ref.parent:
                self.collection_mgr.serialize_one_item(ref.parent)

            if with_sync:
                if isinstance(ref, system.System):
                    # we don't need openvz containers to be network bootable
                    if ref.virt_type == "openvz":
                        ref.netboot_enabled = False
                    self.lite_sync.add_single_system(ref.name)
                    self.api.sync_systems(systems=[ref.name])
                elif isinstance(ref, profile.Profile):
                    # we don't need openvz containers to be network bootable
                    if ref.virt_type == "openvz":
                        ref.enable_menu = False
                    self.lite_sync.add_single_profile(ref.name)
                    self.api.sync_systems(systems=ref.get_children())
                elif isinstance(ref, distro.Distro):
                    self.lite_sync.add_single_distro(ref.name)
                elif isinstance(ref, image.Image):
                    self.lite_sync.add_single_image(ref.name)
                elif isinstance(ref, repo.Repo):
                    pass
                elif isinstance(ref, mgmtclass.Mgmtclass):
                    pass
                elif isinstance(ref, package.Package):
                    pass
                elif isinstance(ref, file.File):
                    pass
                elif isinstance(ref, menu.Menu):
                    pass
                else:
                    print(f"Internal error. Object type not recognized: {type(ref)}")
            if not with_sync and quick_pxe_update:
                if isinstance(ref, system.System):
                    self.lite_sync.update_system_netboot_status(ref.name)

            # save the tree, so if neccessary, scripts can examine it.
            if with_triggers:
                utils.run_triggers(
                    self.api, ref, "/var/lib/cobbler/triggers/change/*", []
                )
                utils.run_triggers(
                    self.api,
                    ref,
                    f"/var/lib/cobbler/triggers/add/{self.collection_type()}/post/*",
                    [],
                )

    def to_string(self) -> str:
        """
        Creates a printable representation of the collection suitable for reading by humans or parsing from scripts.
        Actually scripts would be better off reading the JSON in the cobbler_collections files directly.

        :return: The object as a string representation.
        """
        # FIXME: No to_string() method in any of the items present!
        values = list(self.listing.values())[:]  # copy the values
        values.sort()  # sort the copy (2.3 fix)
        results = []
        for _, value in enumerate(values):
            results.append(value.to_string())
        if len(values) > 0:
            return "\n\n".join(results)
        return "No objects found"

    @staticmethod
    def collection_type() -> str:
        """
        Returns the string key for the name of the collection (used by serializer etc)
        """
        raise NotImplementedError(
            'Please implement the method "collection_type" in your Collection!'
        )

    @staticmethod
    def collection_types() -> str:
        """
        Returns the string key for the plural name of the collection (used by serializer)
        """
        raise NotImplementedError(
            'Please implement the method "collection_types" in your Collection!'
        )

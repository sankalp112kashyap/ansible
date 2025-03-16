# (c) 2012-2014, Michael DeHaan <michael.dehaan@gmail.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import annotations

import os

from ansible import constants as C
from ansible.errors import AnsibleParserError, AnsibleUndefinedVariable, AnsibleAssertionError
from ansible.module_utils.common.text.converters import to_native
from ansible.parsing.mod_args import ModuleArgsParser
from ansible.utils.display import Display

display = Display()


def _validate_ds_type(ds, expected_type, error_prefix=""):
    if not isinstance(ds, expected_type):
        raise AnsibleAssertionError(f'{error_prefix}ds ({ds}) should be a {expected_type} but was a {type(ds)}')

def _get_templar(loader, variable_manager, play, task):
    all_vars = variable_manager.get_vars(play=play, task=task)
    return Templar(loader=loader, variables=all_vars)

def load_list_of_blocks(ds, play, parent_block=None, role=None, task_include=None, use_handlers=False, variable_manager=None, loader=None):
    from ansible.playbook.block import Block
    
    _validate_ds_type(ds, (list, type(None)))
    
    if not ds:
        return []
        
    block_list = []
    count = iter(range(len(ds)))
    
    for i in count:
        block_ds = ds[i]
        implicit_blocks = []
        
        # Collect consecutive implicit blocks
        while block_ds and not Block.is_block(block_ds):
            implicit_blocks.append(block_ds)
            i += 1
            next(count, None)
            block_ds = ds[i] if i < len(ds) else None
            
        # Process both implicit blocks and explicit block
        for b in (implicit_blocks, block_ds):
            if b:
                block_list.append(
                    Block.load(b, play=play, parent_block=parent_block, role=role,
                             task_include=task_include, use_handlers=use_handlers,
                             variable_manager=variable_manager, loader=loader)
                )
                
    return block_list

def load_list_of_tasks(ds, play, block=None, role=None, task_include=None, use_handlers=False, variable_manager=None, loader=None):
    from ansible.playbook.block import Block
    from ansible.playbook.handler import Handler
    from ansible.playbook.task import Task
    from ansible.playbook.task_include import TaskInclude
    from ansible.playbook.role_include import IncludeRole
    from ansible.playbook.handler_task_include import HandlerTaskInclude
    
    _validate_ds_type(ds, list)
    
    def handle_imports(task_ds, include_class):
        t = include_class.load(task_ds, block=block, role=role, task_include=None,
                             variable_manager=variable_manager, loader=loader)
        
        if action in C._ACTION_IMPORT_TASKS:
            if t.loop is not None:
                raise AnsibleParserError("You cannot use loops on 'import_tasks' statements. Use 'include_tasks' instead.", obj=task_ds)
            return _handle_static_import(t, task_ds, play, block, role, use_handlers, loader, variable_manager)
        return [t]
    
    task_list = []
    for task_ds in ds:
        _validate_ds_type(task_ds, dict)
        
        if 'block' in task_ds:
            if use_handlers:
                raise AnsibleParserError("Using a block as a handler is not supported.", obj=task_ds)
            task_list.append(Block.load(task_ds, play=play, parent_block=block, role=role,
                                      task_include=task_include, use_handlers=use_handlers,
                                      variable_manager=variable_manager, loader=loader))
            continue
            
        args_parser = ModuleArgsParser(task_ds)
        try:
            action, args, delegate_to = args_parser.parse(skip_action_validation=True)
        except AnsibleParserError as e:
            raise AnsibleParserError(to_native(e), obj=task_ds, orig_exc=e) if not e.obj else e
            
        if action in C._ACTION_ALL_INCLUDE_IMPORT_TASKS:
            include_class = HandlerTaskInclude if use_handlers else TaskInclude
            task_list.extend(handle_imports(task_ds, include_class))
            
        elif action in C._ACTION_ALL_PROPER_INCLUDE_IMPORT_ROLES:
            if use_handlers:
                raise AnsibleParserError(f"Using '{action}' as a handler is not supported.", obj=task_ds)
            task_list.extend(_handle_role_import(task_ds, action, play, block, role, loader, variable_manager))
            
        else:
            task_class = Handler if use_handlers else Task
            t = task_class.load(task_ds, block=block, role=role, task_include=task_include,
                              variable_manager=variable_manager, loader=loader)
            
            if t.action in C._ACTION_META and t.args.get('_raw_params') == "end_role":
                if use_handlers:
                    raise AnsibleParserError("Cannot execute 'end_role' from a handler")
                if role is None:
                    raise AnsibleParserError("Cannot execute 'end_role' from outside of a role")
                    
            task_list.append(t)
            
    return task_list

def load_list_of_roles(ds, play, current_role_path=None, variable_manager=None, loader=None, collection_search_list=None):
    """
    Loads and returns a list of RoleInclude objects from the ds list of role definitions
    :param ds: list of roles to load
    :param play: calling Play object
    :param current_role_path: path of the owning role, if any
    :param variable_manager: varmgr to use for templating
    :param loader: loader to use for DS parsing/services
    :param collection_search_list: list of collections to search for unqualified role names
    :return:
    """
    # we import here to prevent a circular dependency with imports
    from ansible.playbook.role.include import RoleInclude

    _validate_ds_type(ds, list)

    roles = []
    for role_def in ds:
        i = RoleInclude.load(role_def, play=play, current_role_path=current_role_path, variable_manager=variable_manager,
                             loader=loader, collection_list=collection_search_list)
        roles.append(i)

    return roles

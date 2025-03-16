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


def load_list_of_blocks(ds, play, parent_block=None, role=None, task_include=None, use_handlers=False, variable_manager=None, loader=None):
    from ansible.playbook.block import Block

    if not isinstance(ds, (list, type(None))):
        raise AnsibleAssertionError(f'{ds} should be a list or None but is {type(ds)}')

    block_list = []
    if ds:
        count = iter(range(len(ds)))
        for i in count:
            block_ds = ds[i]
            implicit_blocks = []
            while block_ds is not None and not Block.is_block(block_ds):
                implicit_blocks.append(block_ds)
                i += 1
                next(count, None)
                block_ds = ds[i] if i < len(ds) else None

            for b in (implicit_blocks, block_ds):
                if b:
                    block_list.append(Block.load(b, play=play, parent_block=parent_block, role=role, task_include=task_include, use_handlers=use_handlers, variable_manager=variable_manager, loader=loader))

    return block_list

def load_list_of_tasks(ds, play, block=None, role=None, task_include=None, use_handlers=False, variable_manager=None, loader=None):
    from ansible.playbook.block import Block
    from ansible.playbook.handler import Handler
    from ansible.playbook.task import Task
    from ansible.playbook.task_include import TaskInclude
    from ansible.playbook.role_include import IncludeRole
    from ansible.playbook.handler_task_include import HandlerTaskInclude
    from ansible.template import Templar

    if not isinstance(ds, list):
        raise AnsibleAssertionError(f'The ds ({ds}) should be a list but was a {type(ds)}')

    task_list = []
    for task_ds in ds:
        if not isinstance(task_ds, dict):
            raise AnsibleAssertionError(f'The ds ({ds}) should be a dict but was a {type(ds)}')

        if 'block' in task_ds:
            if use_handlers:
                raise AnsibleParserError("Using a block as a handler is not supported.", obj=task_ds)
            t = Block.load(task_ds, play=play, parent_block=block, role=role, task_include=task_include, use_handlers=use_handlers, variable_manager=variable_manager, loader=loader)
            task_list.append(t)
        else:
            args_parser = ModuleArgsParser(task_ds)
            try:
                action, args, delegate_to = args_parser.parse(skip_action_validation=True)
            except AnsibleParserError as e:
                if e.obj:
                    raise
                raise AnsibleParserError(to_native(e), obj=task_ds, orig_exc=e)

            if action in C._ACTION_ALL_INCLUDE_IMPORT_TASKS:
                include_class = HandlerTaskInclude if use_handlers else TaskInclude
                t = include_class.load(task_ds, block=block, role=role, task_include=None, variable_manager=variable_manager, loader=loader)
                all_vars = variable_manager.get_vars(play=play, task=t)
                templar = Templar(loader=loader, variables=all_vars)

                if action in C._ACTION_IMPORT_TASKS:
                    if t.loop is not None:
                        raise AnsibleParserError("You cannot use loops on 'import_tasks' statements. You should use 'include_tasks' instead.", obj=task_ds)
                    t.statically_loaded = True
                    parent_include = block
                    cumulative_path = None
                    found = False
                    subdir = 'handlers' if use_handlers else 'tasks'
                    while parent_include is not None:
                        if not isinstance(parent_include, TaskInclude):
                            parent_include = parent_include._parent
                            continue
                        try:
                            parent_include_dir = os.path.dirname(templar.template(parent_include.args.get('_raw_params')))
                        except AnsibleUndefinedVariable as e:
                            if not parent_include.statically_loaded:
                                raise AnsibleParserError(f"Error when evaluating variable in dynamic parent include path: {parent_include.args.get('_raw_params')}. When using static imports, the parent dynamic include cannot utilize host facts or variables from inventory", obj=task_ds, suppress_extended_error=True, orig_exc=e)
                            raise
                        cumulative_path = parent_include_dir if cumulative_path is None else os.path.join(parent_include_dir, cumulative_path)
                        include_target = templar.template(t.args['_raw_params'])
                        include_file = loader.path_dwim_relative(t._role._role_path if t._role else loader.get_basedir(), subdir, include_target) if t._role else loader.path_dwim(include_target)
                        if os.path.exists(include_file):
                            found = True
                            break
                        parent_include = parent_include._parent

                    if not found:
                        include_target = templar.template(t.args['_raw_params'])
                        include_file = loader.path_dwim_relative(t._role._role_path if t._role else loader.get_basedir(), subdir, include_target) if t._role else loader.path_dwim(include_target)

                    data = loader.load_from_file(include_file)
                    if not data:
                        display.warning(f'file {include_file} is empty and had no tasks to include')
                        continue
                    if not isinstance(data, list):
                        raise AnsibleParserError("included task files must contain a list of tasks", obj=data)

                    display.vv(f"statically imported: {include_file}")
                    ti_copy = t.copy(exclude_parent=True)
                    ti_copy._parent = block
                    included_blocks = load_list_of_blocks(data, play=play, parent_block=None, task_include=ti_copy, role=role, use_handlers=use_handlers, loader=loader, variable_manager=variable_manager)
                    tags = ti_copy.tags[:]
                    for b in included_blocks:
                        b.tags = list(set(b.tags).union(tags))
                    task_list.extend(b.block if use_handlers else included_blocks)
                else:
                    task_list.append(t)

            elif action in C._ACTION_ALL_PROPER_INCLUDE_IMPORT_ROLES:
                if use_handlers:
                    raise AnsibleParserError(f"Using '{action}' as a handler is not supported.", obj=task_ds)
                ir = IncludeRole.load(task_ds, block=block, role=role, task_include=None, variable_manager=variable_manager, loader=loader)
                if action in C._ACTION_IMPORT_ROLE:
                    if ir.loop is not None:
                        raise AnsibleParserError("You cannot use loops on 'import_role' statements. You should use 'include_role' instead.", obj=task_ds)
                    ir.statically_loaded = True
                    all_vars = variable_manager.get_vars(play=play, task=ir)
                    templar = Templar(loader=loader, variables=all_vars)
                    ir.post_validate(templar=templar)
                    ir._role_name = templar.template(ir._role_name)
                    blocks, _ = ir.get_block_list(variable_manager=variable_manager, loader=loader)
                    task_list.extend(blocks)
                else:
                    task_list.append(ir)
            else:
                t = Handler.load(task_ds, block=block, role=role, task_include=task_include, variable_manager=variable_manager, loader=loader) if use_handlers else Task.load(task_ds, block=block, role=role, task_include=task_include, variable_manager=variable_manager, loader=loader)
                if t.action in C._ACTION_META and t.args.get('_raw_params') == "end_role" and (use_handlers or role is None):
                    raise AnsibleParserError("Cannot execute 'end_role' from a handler" if use_handlers else "Cannot execute 'end_role' from outside of a role")
                task_list.append(t)

    return task_list

def load_list_of_roles(ds, play, current_role_path=None, variable_manager=None, loader=None, collection_search_list=None):
    from ansible.playbook.role.include import RoleInclude

    if not isinstance(ds, list):
        raise AnsibleAssertionError(f'ds ({ds}) should be a list but was a {type(ds)}')

    roles = [RoleInclude.load(role_def, play=play, current_role_path=current_role_path, variable_manager=variable_manager, loader=loader, collection_list=collection_search_list) for role_def in ds]

    return roles

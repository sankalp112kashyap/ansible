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

import ansible.constants as C
from ansible.errors import AnsibleParserError
from ansible.module_utils.common.sentinel import Sentinel
from ansible.playbook.attribute import NonInheritableFieldAttribute
from ansible.playbook.base import Base
from ansible.playbook.conditional import Conditional
from ansible.playbook.collectionsearch import CollectionSearch
from ansible.playbook.delegatable import Delegatable
from ansible.playbook.helpers import load_list_of_tasks
from ansible.playbook.notifiable import Notifiable
from ansible.playbook.role import Role
from ansible.playbook.taggable import Taggable


class Block(Base, Conditional, CollectionSearch, Taggable, Notifiable, Delegatable):

    # main block fields containing the task lists
    block = NonInheritableFieldAttribute(isa='list', default=list)
    rescue = NonInheritableFieldAttribute(isa='list', default=list)
    always = NonInheritableFieldAttribute(isa='list', default=list)

    def __init__(self, play=None, parent_block=None, role=None, task_include=None, use_handlers=False, implicit=False):
        self._play = play
        self._role = role
        self._parent = parent_block or task_include
        self._dep_chain = None
        self._use_handlers = use_handlers
        self._implicit = implicit

        super(Block, self).__init__()

    def __repr__(self):
        return f"BLOCK(uuid={self._uuid})(id={id(self)})(parent={self._parent})"

    def __eq__(self, other):
        return self._uuid == other._uuid

    def __ne__(self, other):
        return self._uuid != other._uuid

    def get_vars(self):
        all_vars = self.vars.copy()
        if self._parent:
            all_vars |= self._parent.get_vars()
        return all_vars

    @staticmethod
    def load(data, play=None, parent_block=None, role=None, task_include=None, use_handlers=False, variable_manager=None, loader=None):
        implicit = not Block.is_block(data)
        b = Block(play=play, parent_block=parent_block, role=role, task_include=task_include, use_handlers=use_handlers, implicit=implicit)
        return b.load_data(data, variable_manager=variable_manager, loader=loader)

    @staticmethod
    def is_block(ds):
        return isinstance(ds, dict) and any(attr in ds for attr in ('block', 'rescue', 'always'))

    def preprocess_data(self, ds):
        if not Block.is_block(ds):
            ds = dict(block=[ds] if not isinstance(ds, list) else ds)
        return super(Block, self).preprocess_data(ds)

    def _load_tasks(self, attr, ds):
        try:
            return load_list_of_tasks(
                ds,
                play=self._play,
                block=self,
                role=self._role,
                task_include=None,
                variable_manager=self._variable_manager,
                loader=self._loader,
                use_handlers=self._use_handlers,
            )
        except AssertionError as e:
            raise AnsibleParserError(f"A malformed block was encountered while loading {attr}.", obj=self._ds, orig_exc=e)

    _load_block = _load_rescue = _load_always = _load_tasks

    def _validate_always(self, attr, name, value):
        if value and not self.block:
            raise AnsibleParserError(f"'{name}' keyword cannot be used without 'block'", obj=self._ds)

    _validate_rescue = _validate_always

    def get_dep_chain(self):
        if self._dep_chain is None and self._parent:
            return self._parent.get_dep_chain()
        return self._dep_chain[:] if self._dep_chain else None

    def copy(self, exclude_parent=False, exclude_tasks=False):
        def _dupe_task_list(task_list, new_block):
            return [
                task.copy(exclude_parent=True, _parent=new_block if task._parent == new_block else task._parent.copy(exclude_tasks=True)
                for task in task_list
            ]

        new_me = super(Block, self).copy()
        new_me._play = self._play
        new_me._use_handlers = self._use_handlers
        new_me._dep_chain = self._dep_chain[:] if self._dep_chain else None
        new_me._parent = self._parent.copy(exclude_tasks=True) if self._parent and not exclude_parent else None
        new_me._role = self._role

        if not exclude_tasks:
            new_me.block = _dupe_task_list(self.block or [], new_me)
            new_me.rescue = _dupe_task_list(self.rescue or [], new_me)
            new_me.always = _dupe_task_list(self.always or [], new_me)

        new_me.validate()
        return new_me

    def serialize(self):
        data = {attr: getattr(self, attr) for attr in self.fattributes if attr not in ('block', 'rescue', 'always')}
        data['dep_chain'] = self.get_dep_chain()

        if self._role is not None:
            data['role'] = self._role.serialize()
        if self._parent is not None:
            data['parent'] = self._parent.copy(exclude_tasks=True).serialize()
            data['parent_type'] = self._parent.__class__.__name__

        return data

    def deserialize(self, data):
        from ansible.playbook.task_include import TaskInclude
        from ansible.playbook.handler_task_include import HandlerTaskInclude

        for attr in self.fattributes:
            if attr in data and attr not in ('block', 'rescue', 'always'):
                setattr(self, attr, data.get(attr))

        self._dep_chain = data.get('dep_chain', None)

        if 'role' in data:
            self._role = Role()
            self._role.deserialize(data['role'])

        if 'parent' in data:
            parent_type = data.get('parent_type')
            parent_class = {
                'Block': Block,
                'TaskInclude': TaskInclude,
                'HandlerTaskInclude': HandlerTaskInclude,
            }.get(parent_type, Block)
            self._parent = parent_class()
            self._parent.deserialize(data['parent'])
            self._dep_chain = self._parent.get_dep_chain()

    def set_loader(self, loader):
        self._loader = loader
        if self._parent:
            self._parent.set_loader(loader)
        elif self._role:
            self._role.set_loader(loader)

        dep_chain = self.get_dep_chain()
        if dep_chain:
            for dep in dep_chain:
                dep.set_loader(loader)

    def _get_parent_attribute(self, attr, omit=False):
        fattr = self.fattributes[attr]
        value = Sentinel if omit else getattr(self, f'_{attr}', Sentinel)

        if value is Sentinel or fattr.extend:
            for source in [self._parent, self._role, self._play]:
                if source:
                    try:
                        parent_value = getattr(source, f'_{attr}', Sentinel)
                        if parent_value is not Sentinel:
                            value = self._extend_value(value, parent_value, fattr.prepend) if fattr.extend else parent_value
                            if value is not Sentinel and not fattr.extend:
                                break
                    except AttributeError:
                        pass

        return value

    def filter_tagged_tasks(self, all_vars):
        def evaluate_and_append_task(target):
            return [
                evaluate_block(task) if isinstance(task, Block) else task
                for task in target
                if (task.action in C._ACTION_META and task.implicit) or task.evaluate_tags(self._play.only_tags, self._play.skip_tags, all_vars=all_vars)
            ]

        def evaluate_block(block):
            new_block = block.copy(exclude_parent=True, exclude_tasks=True)
            new_block._parent = block._parent
            new_block.block = evaluate_and_append_task(block.block)
            new_block.rescue = evaluate_and_append_task(block.rescue)
            new_block.always = evaluate_and_append_task(block.always)
            return new_block

        return evaluate_block(self)

    def get_tasks(self):
        def evaluate_and_append_task(target):
            return [
                task if not isinstance(task, Block) else evaluate_block(task)
                for task in target
            ]

        def evaluate_block(block):
            return evaluate_and_append_task(block.block) + evaluate_and_append_task(block.rescue) + evaluate_and_append_task(block.always)

        return evaluate_block(self)

    def has_tasks(self):
        return bool(self.block or self.rescue or self.always)

    def get_include_params(self):
        return self._parent.get_include_params() if self._parent else dict()

    def all_parents_static(self):
        from ansible.playbook.task_include import TaskInclude
        return not (self._parent and isinstance(self._parent, TaskInclude) and not self._parent.statically_loaded) and (not self._parent or self._parent.all_parents_static())

    def get_first_parent_include(self):
        from ansible.playbook.task_include import TaskInclude
        return self._parent.get_first_parent_include() if self._parent and isinstance(self._parent, TaskInclude) else None
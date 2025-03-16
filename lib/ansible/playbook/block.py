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

    # for future consideration? this would be functionally
    # similar to the 'else' clause for exceptions
    # otherwise = FieldAttribute(isa='list')

    def __init__(self, play=None, parent_block=None, role=None, task_include=None, use_handlers=False, implicit=False):
        self._play = play
        self._role = role
        self._parent = task_include or parent_block
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
        all_vars = {}
        if self._parent:
            all_vars |= self._parent.get_vars()
        all_vars |= self.vars.copy()
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
            ds = dict(block=[ds] if isinstance(ds, dict) else ds)
        return super(Block, self).preprocess_data(ds)

    def _load_block(self, attr, ds):
        try:
            return load_list_of_tasks(ds, play=self._play, block=self, role=self._role, task_include=None, variable_manager=self._variable_manager, loader=self._loader, use_handlers=self._use_handlers)
        except AssertionError as e:
            raise AnsibleParserError("A malformed block was encountered while loading a block", obj=self._ds, orig_exc=e)

    _load_rescue = _load_block
    _load_always = _load_block

    def _validate_always(self, attr, name, value):
        if value and not self.block:
            raise AnsibleParserError(f"'{name}' keyword cannot be used without 'block'", obj=self._ds)

    _validate_rescue = _validate_always

    def get_dep_chain(self):
        return self._dep_chain[:] if self._dep_chain else self._parent.get_dep_chain() if self._parent else None

    def copy(self, exclude_parent=False, exclude_tasks=False):
        def _dupe_task_list(task_list, new_block):
            new_task_list = []
            for task in task_list:
                new_task = task.copy(exclude_parent=True)
                new_task._parent = new_block if task._parent == new_block else task._parent.copy(exclude_tasks=True)
                new_task_list.append(new_task)
            return new_task_list

        new_me = super(Block, self).copy()
        new_me._play = self._play
        new_me._use_handlers = self._use_handlers
        new_me._dep_chain = self._dep_chain[:]
        new_me._parent = self._parent.copy(exclude_tasks=True) if self._parent and not exclude_parent else None
        if not exclude_tasks:
            new_me.block = _dupe_task_list(self.block or [], new_me)
            new_me.rescue = _dupe_task_list(self.rescue or [], new_me)
            new_me.always = _dupe_task_list(self.always or [], new_me)
        new_me._role = self._role
        new_me.validate()
        return new_me

    def serialize(self):
        data = {attr: getattr(self, attr) for attr in self.fattributes if attr not in ('block', 'rescue', 'always')}
        data['dep_chain'] = self.get_dep_chain()
        if self._role:
            data['role'] = self._role.serialize()
        if self._parent:
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

        role_data = data.get('role')
        if role_data:
            self._role = Role().deserialize(role_data)

        parent_data = data.get('parent')
        if parent_data:
            parent_type = data.get('parent_type')
            parent_class = {'Block': Block, 'TaskInclude': TaskInclude, 'HandlerTaskInclude': HandlerTaskInclude}.get(parent_type)
            if parent_class:
                self._parent = parent_class().deserialize(parent_data)
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
        extend = fattr.extend
        prepend = fattr.prepend

        value = Sentinel if omit else getattr(self, f'_{attr}', Sentinel)
        _parent = self._parent if getattr(self._parent, 'statically_loaded', True) else self._parent._parent

        if _parent and (value is Sentinel or extend):
            parent_value = _parent._get_parent_attribute(attr) if hasattr(_parent, '_get_parent_attribute') else getattr(_parent, f'_{attr}', Sentinel)
            value = self._extend_value(value, parent_value, prepend) if extend else parent_value

        if self._role and (value is Sentinel or extend):
            parent_value = getattr(self._role, f'_{attr}', Sentinel)
            value = self._extend_value(value, parent_value, prepend) if extend else parent_value

            dep_chain = self.get_dep_chain()
            if dep_chain and (value is Sentinel or extend):
                for dep in reversed(dep_chain):
                    dep_value = getattr(dep, f'_{attr}', Sentinel)
                    value = self._extend_value(value, dep_value, prepend) if extend else dep_value
                    if value is not Sentinel and not extend:
                        break

        if self._play and (value is Sentinel or extend):
            play_value = getattr(self._play, f'_{attr}', Sentinel)
            value = self._extend_value(value, play_value, prepend) if extend else play_value

        return value

    def filter_tagged_tasks(self, all_vars):
        def evaluate_and_append_task(target):
            tmp_list = []
            for task in target:
                if isinstance(task, Block):
                    filtered_block = evaluate_block(task)
                    if filtered_block.has_tasks():
                        tmp_list.append(filtered_block)
                elif ((task.action in C._ACTION_META and task.implicit) or task.evaluate_tags(self._play.only_tags, self._play.skip_tags, all_vars=all_vars)):
                    tmp_list.append(task)
            return tmp_list

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
            tmp_list = []
            for task in target:
                if isinstance(task, Block):
                    tmp_list.extend(evaluate_block(task))
                else:
                    tmp_list.append(task)
            return tmp_list

        def evaluate_block(block):
            rv = evaluate_and_append_task(block.block)
            rv.extend(evaluate_and_append_task(block.rescue))
            rv.extend(evaluate_and_append_task(block.always))
            return rv

        return evaluate_block(self)

    def has_tasks(self):
        return bool(self.block or self.rescue or self.always)

    def get_include_params(self):
        return self._parent.get_include_params() if self._parent else dict()

    def all_parents_static(self):
        from ansible.playbook.task_include import TaskInclude
        return not (self._parent and isinstance(self._parent, TaskInclude) and not self._parent.statically_loaded) and (self._parent.all_parents_static() if self._parent else True)

    def get_first_parent_include(self):
        from ansible.playbook.task_include import TaskInclude
        return self._parent.get_first_parent_include() if self._parent and not isinstance(self._parent, TaskInclude) else self._parent

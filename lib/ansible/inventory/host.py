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

from collections.abc import Mapping, MutableMapping

from ansible.inventory.group import Group, InventoryObjectType
from ansible.parsing.utils.addresses import patterns
from ansible.utils.vars import combine_vars, get_unique_id


__all__ = ['Host']


class Host:
    """ a single ansible host """
    base_type = InventoryObjectType.HOST

    def __getstate__(self):
        return self.serialize()

    def __setstate__(self, data):
        self.deserialize(data)

    def __eq__(self, other):
        return isinstance(other, Host) and self._uuid == other._uuid

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(self.name)

    def __str__(self):
        return self.get_name()

    def __repr__(self):
        return self.get_name()

    def serialize(self):
        return dict(
            name=self.name,
            vars=self.vars.copy(),
            address=self.address,
            uuid=self._uuid,
            groups=[group.serialize() for group in self.groups],
            implicit=self.implicit,
        )

    def deserialize(self, data):
        self.__init__(gen_uuid=False)  # used by __setstate__ to deserialize in place  # pylint: disable=unnecessary-dunder-call
        self.name = data.get('name')
        self.vars = data.get('vars', {})
        self.address = data.get('address', '')
        self._uuid = data.get('uuid')
        self.implicit = data.get('implicit', False)
        self.groups = [Group().deserialize(group_data) for group_data in data.get('groups', [])]

    def __init__(self, name=None, port=None, gen_uuid=True):
        self.vars = {}
        self.groups = []
        self._uuid = get_unique_id() if gen_uuid else None
        self.name = self.address = name
        if port:
            self.set_variable('ansible_port', int(port))
        self.implicit = False

    def get_name(self):
        return self.name

    def populate_ancestors(self, additions=None):
        groups_to_add = additions if additions is not None else self.groups
        for group in groups_to_add:
            if group not in self.groups:
                self.groups.append(group)

    def add_group(self, group):
        added = False
        for oldg in group.get_ancestors():
            if oldg not in self.groups:
                self.groups.append(oldg)
        if group not in self.groups:
            self.groups.append(group)
            added = True
        return added

    def remove_group(self, group):
        removed = False
        if group in self.groups:
            self.groups.remove(group)
            removed = True
            for oldg in group.get_ancestors():
                if oldg.name != 'all' and not any(oldg in childg.get_ancestors() for childg in self.groups):
                    self.remove_group(oldg)
        return removed

    def set_variable(self, key, value):
        if key in self.vars and isinstance(self.vars[key], MutableMapping) and isinstance(value, Mapping):
            self.vars = combine_vars(self.vars, {key: value})
        else:
            self.vars[key] = value

    def get_groups(self):
        return self.groups

    def get_magic_vars(self):
        results = {
            'inventory_hostname': self.name,
            'inventory_hostname_short': self.name if patterns['ipv4'].match(self.name) or patterns['ipv6'].match(self.name) else self.name.split('.')[0],
            'group_names': sorted(g.name for g in self.get_groups() if g.name != 'all')
        }
        return results

    def get_vars(self):
        return combine_vars(self.vars, self.get_magic_vars())

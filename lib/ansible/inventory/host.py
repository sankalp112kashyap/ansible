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

    def __init__(self, name=None, port=None, gen_uuid=True):
        self.name = name
        self.address = name
        self.vars = {}
        self.groups = []
        self._uuid = get_unique_id() if gen_uuid else None
        self.implicit = False
        
        if port:
            self.set_variable('ansible_port', int(port))

    def __eq__(self, other):
        return isinstance(other, Host) and self._uuid == other._uuid

    def __hash__(self):
        return hash(self.name)

    def __str__(self):
        return self.name

    __repr__ = __str__

    def populate_ancestors(self, additions=None):
        if additions:
            self.groups.extend(g for g in additions if g not in self.groups)
        else:
            for group in self.groups:
                ancestors = set(group.get_ancestors()) - set(self.groups)
                self.groups.extend(ancestors)

    def add_group(self, group):
        ancestors = set(group.get_ancestors()) - set(self.groups)
        self.groups.extend(ancestors)
        if group not in self.groups:
            self.groups.append(group)
            return True
        return False

    def remove_group(self, group):
        if group not in self.groups:
            return False
            
        self.groups.remove(group)
        
        # Remove exclusive ancestors except 'all'
        ancestors = set(group.get_ancestors())
        for ancestor in ancestors:
            if ancestor.name == 'all':
                continue
            if not any(ancestor in g.get_ancestors() for g in self.groups):
                self.remove_group(ancestor)
        return True

    def set_variable(self, key, value):
        if isinstance(self.vars.get(key), MutableMapping) and isinstance(value, Mapping):
            self.vars = combine_vars(self.vars, {key: value})
        else:
            self.vars[key] = value

    def get_magic_vars(self):
        hostname_short = self.name if patterns['ipv4'].match(self.name) or patterns['ipv6'].match(self.name) else self.name.split('.')[0]
        
        return {
            'inventory_hostname': self.name,
            'inventory_hostname_short': hostname_short,
            'group_names': sorted(g.name for g in self.groups if g.name != 'all')
        }

    def get_vars(self):
        return combine_vars(self.vars, self.get_magic_vars())

    # Maintain serialization for compatibility
    def serialize(self):
        return {
            'name': self.name,
            'vars': self.vars.copy(),
            'address': self.address,
            'uuid': self._uuid,
            'groups': [g.serialize() for g in self.groups],
            'implicit': self.implicit,
        }

    def deserialize(self, data):
        self.__init__(gen_uuid=False)
        self.name = data.get('name')
        self.vars = data.get('vars', {})
        self.address = data.get('address', '')
        self._uuid = data.get('uuid')
        self.implicit = data.get('implicit', False)
        
        for group_data in data.get('groups', []):
            g = Group()
            g.deserialize(group_data)
            self.groups.append(g)

    __getstate__ = serialize
    __setstate__ = deserialize

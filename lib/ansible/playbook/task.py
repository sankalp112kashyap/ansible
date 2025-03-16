from __future__ import annotations

from ansible import constants as C
from ansible.errors import AnsibleError, AnsibleParserError, AnsibleUndefinedVariable, AnsibleAssertionError
from ansible.module_utils.common.sentinel import Sentinel
from ansible.module_utils.common.text.converters import to_native
from ansible.module_utils.six import string_types
from ansible.parsing.mod_args import ModuleArgsParser
from ansible.parsing.yaml.objects import AnsibleBaseYAMLObject, AnsibleMapping
from ansible.plugins.loader import lookup_loader
from ansible.playbook.attribute import NonInheritableFieldAttribute
from ansible.playbook.base import Base
from ansible.playbook.block import Block
from ansible.playbook.collectionsearch import CollectionSearch
from ansible.playbook.conditional import Conditional
from ansible.playbook.delegatable import Delegatable
from ansible.playbook.loop_control import LoopControl
from ansible.playbook.notifiable import Notifiable
from ansible.playbook.role import Role
from ansible.playbook.taggable import Taggable
from ansible.utils.collection_loader import AnsibleCollectionConfig
from ansible.utils.display import Display
from ansible.utils.vars import isidentifier

__all__ = ['Task']

display = Display()

class Task(Base, Conditional, Taggable, CollectionSearch, Notifiable, Delegatable):

    args = NonInheritableFieldAttribute(isa='dict', default=dict)
    action = NonInheritableFieldAttribute(isa='string')
    async_val = NonInheritableFieldAttribute(isa='int', default=0, alias='async')
    changed_when = NonInheritableFieldAttribute(isa='list', default=list)
    delay = NonInheritableFieldAttribute(isa='float', default=5)
    failed_when = NonInheritableFieldAttribute(isa='list', default=list)
    loop = NonInheritableFieldAttribute(isa='list')
    loop_control = NonInheritableFieldAttribute(isa='class', class_type=LoopControl, default=LoopControl)
    poll = NonInheritableFieldAttribute(isa='int', default=C.DEFAULT_POLL_INTERVAL)
    register = NonInheritableFieldAttribute(isa='string', static=True)
    retries = NonInheritableFieldAttribute(isa='int')
    until = NonInheritableFieldAttribute(isa='list', default=list)
    loop_with = NonInheritableFieldAttribute(isa='string', private=True)

    def __init__(self, block=None, role=None, task_include=None):
        self._role = role
        self._parent = task_include if task_include else block
        self.implicit = False
        self.resolved_action = None
        super(Task, self).__init__()

    def get_name(self, include_role_fqcn=True):
        role_name = self._role.get_name(include_role_fqcn=include_role_fqcn) if self._role else None
        if self._role and self.name:
            return f"{role_name} : {self.name}"
        elif self.name:
            return self.name
        return f"{role_name} : {self.action}" if self._role else f"{self.action}"

    def _merge_kv(self, ds):
        if isinstance(ds, dict):
            return " ".join(f"{k}={v}" for k, v in ds.items() if not k.startswith('_'))
        return ds if isinstance(ds, string_types) else ""

    @staticmethod
    def load(data, block=None, role=None, task_include=None, variable_manager=None, loader=None):
        t = Task(block=block, role=role, task_include=task_include)
        return t.load_data(data, variable_manager=variable_manager, loader=loader)

    def __repr__(self):
        if self.action in C._ACTION_META:
            return f"TASK: meta ({self.args['_raw_params']})"
        return f"TASK: {self.get_name()}"

    def _preprocess_with_loop(self, ds, new_ds, k, v):
        loop_name = k.removeprefix("with_")
        if new_ds.get('loop') or new_ds.get('loop_with'):
            raise AnsibleError(f"duplicate loop in task: {loop_name}", obj=ds)
        if v is None:
            raise AnsibleError(f"you must specify a value when using {k}", obj=ds)
        new_ds['loop_with'] = loop_name
        new_ds['loop'] = v

    def preprocess_data(self, ds):
        if not isinstance(ds, dict):
            raise AnsibleAssertionError(f'ds ({ds}) should be a dict but was a {type(ds)}')

        new_ds = AnsibleMapping()
        if isinstance(ds, AnsibleBaseYAMLObject):
            new_ds.ansible_pos = ds.ansible_pos

        default_collection = AnsibleCollectionConfig.default_collection
        collections_list = ds.get('collections', self.collections)
        if collections_list is not None:
            collections_list = self.get_validated_value('collections', self.fattributes.get('collections'), collections_list, None)

        if default_collection and not self._role:
            if collections_list and default_collection not in collections_list:
                collections_list.insert(0, default_collection)
            else:
                collections_list = [default_collection]

        if collections_list and 'ansible.builtin' not in collections_list and 'ansible.legacy' not in collections_list:
            collections_list.append('ansible.legacy')

        if collections_list:
            ds['collections'] = collections_list

        args_parser = ModuleArgsParser(task_ds=ds, collection_list=collections_list)
        try:
            action, args, delegate_to = args_parser.parse()
        except AnsibleParserError as e:
            raise AnsibleParserError(to_native(e), obj=ds, orig_exc=e) if not e.obj else e
        else:
            self.resolved_action = args_parser.resolved_action

        if action in C._ACTION_HAS_CMD and 'cmd' in args:
            if args.get('_raw_params', ''):
                raise AnsibleError("The 'cmd' argument cannot be used when other raw parameters are specified.", obj=ds)
            args['_raw_params'] = args.pop('cmd')

        new_ds.update({
            'action': action,
            'args': args,
            'delegate_to': delegate_to,
            'vars': self._load_vars(None, ds.get('vars')) if 'vars' in ds else {}
        })

        for k, v in ds.items():
            if k in ('action', 'local_action', 'args', 'delegate_to', action, 'shell'):
                continue
            elif k.startswith('with_') and k.removeprefix("with_") in lookup_loader:
                self._preprocess_with_loop(ds, new_ds, k, v)
            elif C.INVALID_TASK_ATTRIBUTE_FAILED or k in self.fattributes:
                new_ds[k] = v
            else:
                display.warning(f"Ignoring invalid attribute: {k}")

        return super(Task, self).preprocess_data(new_ds)

    def _load_loop_control(self, attr, ds):
        if not isinstance(ds, dict):
            raise AnsibleParserError("the `loop_control` value must be specified as a dictionary.", obj=ds)
        return LoopControl.load(data=ds, variable_manager=self._variable_manager, loader=self._loader)

    def _validate_attributes(self, ds):
        try:
            super(Task, self)._validate_attributes(ds)
        except AnsibleParserError as e:
            e.message += '\nThis error can be suppressed as a warning using the "invalid_task_attribute_failed" configuration'
            raise e

    def _validate_changed_when(self, attr, name, value):
        if not isinstance(value, list):
            setattr(self, name, [value])

    def _validate_failed_when(self, attr, name, value):
        if not isinstance(value, list):
            setattr(self, name, [value])

    def _validate_register(self, attr, name, value):
        if value is not None and not isidentifier(value):
            raise AnsibleParserError(f"Invalid variable name in 'register' specified: '{value}'")

    def post_validate(self, templar):
        if self._parent:
            self._parent.post_validate(templar)
        super(Task, self).post_validate(templar)

    def _post_validate_loop(self, attr, value, templar):
        return value

    def _post_validate_environment(self, attr, value, templar):
        env = {}
        if value is not None:
            def _parse_env_kv(k, v):
                try:
                    env[k] = templar.template(v, convert_bare=False)
                except AnsibleUndefinedVariable as e:
                    if self.action in C._ACTION_FACT_GATHERING and ('ansible_facts.env' in str(e) or 'ansible_env' in str(e)):
                        return
                    raise

            if isinstance(value, list):
                for env_item in value:
                    if isinstance(env_item, dict):
                        for k in env_item:
                            _parse_env_kv(k, env_item[k])
                    else:
                        isdict = templar.template(env_item, convert_bare=False)
                        if isinstance(isdict, dict):
                            env.update(isdict)
                        else:
                            display.warning(f"could not parse environment value, skipping: {value}")
            elif isinstance(value, dict):
                for env_item in value:
                    _parse_env_kv(env_item, value[env_item])
            else:
                env = templar.template(value, convert_bare=False)
        return env

    def _post_validate_changed_when(self, attr, value, templar):
        return value

    def _post_validate_failed_when(self, attr, value, templar):
        return value

    def _post_validate_until(self, attr, value, templar):
        return value

    def get_vars(self):
        all_vars = self._parent.get_vars() if self._parent else {}
        all_vars.update(self.vars)
        all_vars.pop('tags', None)
        all_vars.pop('when', None)
        return all_vars

    def get_include_params(self):
        all_vars = self._parent.get_include_params() if self._parent else {}
        if self.action in C._ACTION_ALL_INCLUDES:
            all_vars.update(self.vars)
        return all_vars

    def copy(self, exclude_parent=False, exclude_tasks=False):
        new_me = super(Task, self).copy()
        new_me._parent = self._parent.copy(exclude_tasks=exclude_tasks) if self._parent and not exclude_parent else None
        new_me._role = self._role
        new_me.implicit = self.implicit
        new_me.resolved_action = self.resolved_action
        new_me._uuid = self._uuid
        return new_me

    def serialize(self):
        data = super(Task, self).serialize()
        if not self._squashed and not self._finalized:
            if self._parent:
                data['parent'] = self._parent.serialize()
                data['parent_type'] = self._parent.__class__.__name__
            if self._role:
                data['role'] = self._role.serialize()
            data['implicit'] = self.implicit
            data['resolved_action'] = self.resolved_action
        return data

    def deserialize(self, data):
        from ansible.playbook.task_include import TaskInclude
        from ansible.playbook.handler_task_include import HandlerTaskInclude

        parent_data = data.get('parent')
        if parent_data:
            parent_type = data.get('parent_type')
            if parent_type == 'Block':
                p = Block()
            elif parent_type == 'TaskInclude':
                p = TaskInclude()
            elif parent_type == 'HandlerTaskInclude':
                p = HandlerTaskInclude()
            p.deserialize(parent_data)
            self._parent = p
            del data['parent']

        role_data = data.get('role')
        if role_data:
            r = Role()
            r.deserialize(role_data)
            self._role = r
            del data['role']

        self.implicit = data.get('implicit', False)
        self.resolved_action = data.get('resolved_action')
        super(Task, self).deserialize(data)

    def set_loader(self, loader):
        self._loader = loader
        if self._parent:
            self._parent.set_loader(loader)

    def _get_parent_attribute(self, attr, omit=False):
        fattr = self.fattributes[attr]
        extend = fattr.extend
        prepend = fattr.prepend
        value = Sentinel if omit else getattr(self, f'_{attr}', Sentinel)

        if self._parent and (value is Sentinel or extend):
            _parent = self._parent if getattr(self._parent, 'statically_loaded', True) else self._parent._parent
            if _parent:
                parent_value = _parent._get_parent_attribute(attr) if attr != 'vars' and hasattr(_parent, '_get_parent_attribute') else getattr(_parent, f'_{attr}', Sentinel)
                value = self._extend_value(value, parent_value, prepend) if extend else parent_value
        return value

    def all_parents_static(self):
        return self._parent.all_parents_static() if self._parent else True

    def get_first_parent_include(self):
        from ansible.playbook.task_include import TaskInclude
        if self._parent:
            return self._parent if isinstance(self._parent, TaskInclude) else self._parent.get_first_parent_include()
        return None

    def get_play(self):
        parent = self._parent
        while not isinstance(parent, Block):
            parent = parent._parent
        return parent._play
# vim: tabstop=4 shiftwidth=4 softtabstop=4

#    Copyright 2013 Rackspace
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import ConfigParser
import re

from oslo.config import cfg
import webob.exc

import glance.api.policy
from glance.common import exception
from glance.common.ordereddict import OrderedDict
from glance.openstack.common import log as logging
from glance.openstack.common import policy

# NOTE(bourke): The default dict_type is collections.OrderedDict in py27, but
# we must set manually for compatibility with py26
CONFIG = ConfigParser.SafeConfigParser(dict_type=OrderedDict)
LOG = logging.getLogger(__name__)

property_opts = [
    cfg.StrOpt('property_protection_file',
               default=None,
               help=_('The location of the property protection file.')),
    cfg.StrOpt('property_protection_rule_format',
               default='roles',
               help=_('This config value indicates whether "roles" or '
                      '"policies" are used in the property protection file.')),
]

CONF = cfg.CONF
CONF.register_opts(property_opts)


def is_property_protection_enabled():
    if CONF.property_protection_file:
        return True
    return False


class PropertyRules(object):

    def __init__(self, policy_enforcer=None):
        self.rules = []
        self.prop_exp_mapping = {}
        self.policies = []
        self.policy_enforcer = policy_enforcer or glance.api.policy.Enforcer()
        self.prop_prot_rule_format = CONF.property_protection_rule_format
        self.prop_prot_rule_format = self.prop_prot_rule_format.lower()
        self._load_rules()

    def _load_rules(self):
        try:
            conf_file = CONF.find_file(CONF.property_protection_file)
            CONFIG.read(conf_file)
        except Exception as e:
            msg = (_("Couldn't find property protection file %s:%s.") %
                    (CONF.property_protection_file, e))
            LOG.error(msg)
            raise exception.InvalidPropertyProtectionConfiguration()

        if self.prop_prot_rule_format not in ['policies', 'roles']:
            msg = _("Invalid value '%s' for 'property_protection_rule_format'"
                    ". The permitted values are 'roles' and 'policies'" %
                    self.prop_prot_rule_format)
            LOG.error(msg)
            raise exception.InvalidPropertyProtectionConfiguration()

        operations = ['create', 'read', 'update', 'delete']
        properties = CONFIG.sections()
        for property_exp in properties:
            property_dict = {}
            compiled_rule = self._compile_rule(property_exp)

            for operation in operations:
                permissions = CONFIG.get(property_exp, operation)
                if permissions:
                    if self.prop_prot_rule_format == 'policies':
                        if ',' in permissions:
                            msg = _("Multiple policies '%s' not allowed for a"
                                    " given operation. Policies can be "
                                    "combined in the policy file" %
                                    permissions)
                            LOG.error(msg)
                            raise exception.\
                                InvalidPropertyProtectionConfiguration()
                        self.prop_exp_mapping[compiled_rule] = property_exp
                        self._add_policy_rules(property_exp, operation,
                                               permissions)
                        permissions = [permissions]
                    else:
                        permissions = [permission.strip() for permission in
                                       permissions.split(',')]
                    property_dict[operation] = permissions
                else:
                    property_dict[operation] = []
                    msg = _(('Property protection on operation %s for rule '
                            '%s is not found. No role will be allowed to '
                            'perform this operation.' %
                            (operation, property_exp)))
                    LOG.warn(msg)

            self.rules.append((compiled_rule, property_dict))

    def _compile_rule(self, rule):
        try:
            return re.compile(rule)
        except Exception as e:
            msg = (_("Encountered a malformed property protection rule %s:%s.")
                   % (rule, e))
            LOG.error(msg)
            raise exception.InvalidPropertyProtectionConfiguration()

    def _add_policy_rules(self, property_exp, action, rule):
        """ Add policy rules to the policy enforcer.
        For example, if the file listed as property_protection_file has:
        [prop_a]
        create = glance_creator
        then the corresponding policy rule would be:
        "prop_a:create": "rule:glance_creator"
        where glance_creator is defined in policy.json. For example:
        "glance:creator": "role:admin or role:glance_create_user"
        """
        rule = "rule:%s" % rule
        rule_name = "%s:%s" % (property_exp, action)
        rule_dict = {}
        rule_dict[rule_name] = policy.parse_rule(rule)
        self.policy_enforcer.add_rules(rule_dict)

    def _check_policy(self, property_exp, action, context):
        try:
            target = ":".join([property_exp, action])
            self.policy_enforcer.enforce(context, target, {})
        except exception.Forbidden:
            return False
        return True

    def check_property_rules(self, property_name, action, context):
        roles = context.roles
        if not self.rules:
            return True

        if action not in ['create', 'read', 'update', 'delete']:
            return False

        for rule_exp, rule in self.rules:
            if rule_exp.search(str(property_name)):
                rule_roles = rule.get(action)
                if rule_roles:
                    if self.prop_prot_rule_format == 'policies':
                        prop_exp_key = self.prop_exp_mapping[rule_exp]
                        return self._check_policy(prop_exp_key, action,
                                                  context)
                    if set(roles).intersection(set(rule_roles)):
                        return True
        return False

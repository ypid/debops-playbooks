#!/usr/bin/env python

# ldap_attr Ansible module
# Copyright (C) 2014 Peter Sagerson <psagers@ignorare.net>
# Homepage: https://bitbucket.org/psagers/ansible-ldap


# Copyright (c) 2014, Peter Sagerson
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# - Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# - Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


from traceback import format_exc

import ldap
import ldap.sasl


DOCUMENTATION = """
---
module: ldap_attr
short_description: Add or remove LDAP attribute values.
description:
    - Add or remove LDAP attribute values.
notes:
    - This only deals with attributes on existing entries. To add or remove
      whole entries, see M(ldap_entry).
    - The default authentication settings will attempt to use a SASL EXTERNAL
      bind over a UNIX domain socket. This works well with the default Ubuntu
      install for example, which includes a cn=peercred,cn=external,cn=auth ACL
      rule allowing root to modify the server configuration. If you need to use
      a simple bind to access your server, pass the credentials in C(bind_dn)
      and C(bind_pw).
    - For C(state=present) and C(state=absent), all value comparisons are
      performed on the server for maximum accuracy. For C(state=exact), values
      have to be compared in Python, which obviously ignores LDAP matching
      rules. This should work out in most cases, but it is theoretically
      possible to see spurious changes when target and actual values are
      semantically identical but lexically distinct.
version_added: null
author: Peter Sagerson
requirements:
    - python-ldap
options:
    dn:
        required: true
        description:
            - The DN of the entry to modify.
    name:
        required: true
        description:
            - The name of the attribute to modify.
    values:
        required: true
        description:
            - The value(s) to add or remove. This can be a string or a list of
              strings. The complex argument format is required in order to pass
              a list of strings (see examples).
    state:
        required: false
        choices: [present, absent, exact]
        default: present
        description:
            - The state of the attribute values. If C(present), all given
              values will be added if they're missing. If C(absent), all given
              values will be removed if present. If C(exact), the set of values
              will be forced to exactly those provided and no others. If
              C(state=exact) and C(values) is empty, all values for this
              attribute will be removed.
    server_uri:
        required: false
        default: ldapi:///
        description:
            - A URI to the LDAP server. The default value lets the underlying
              LDAP client library look for a UNIX domain socket in its default
              location.
    start_tls:
        required: false
        default: false
        description:
            - If true, we'll use the START_TLS LDAP extension.
    bind_dn:
        required: false
        description:
            - A DN to bind with. If this is omitted, we'll try a SASL bind with
              the EXTERNAL mechanism (see note). If this is blank, we'll use an
              anonymous bind.
    bind_pw:
        required: false
        description:
            - The password to use with C(bind_dn).
"""


EXAMPLES = """
# Configure directory number 1 for example.com.
- ldap_attr: dn='olcDatabase={1}hdb,cn=config' name=olcSuffix values='dc=example,dc=com' state=exact
  become: true

# Set up the ACL. The complex argument format is required here to pass a list
# of ACL strings.
- ldap_attr:
  become: true
  args:
    dn: olcDatabase={1}hdb,cn=config
    name: olcAccess
    values:
      - '{0}to attrs=userPassword,shadowLastChange
         by self write
         by anonymous auth
         by dn="cn=admin,dc=example,dc=com" write
         by * none'
      - '{1}to dn.base="dc=example,dc=com"
         by dn="cn=admin,dc=example,dc=com" write
         by * read'
    state: exact

# Declare some indexes.
- ldap_attr: dn='olcDatabase={1}hdb,cn=config' name=olcDbIndex values={{ item }}
  become: true
  with_items:
    - objectClass eq
    - uid eq

# Set up a root user, which we can use later to bootstrap the directory.
- ldap_attr: dn='olcDatabase={1}hdb,cn=config' name={{ item.key }} values={{ item.value }} state=exact
  become: true
  with_dict:
    olcRootDN: 'cn=root,dc=example,dc=com'
    olcRootPW: '{SSHA}mRskON0Stk+5wO5K+MMk2xmakKt8h7eJ'
"""


def main():
    module = AnsibleModule(
        argument_spec={
            'dn': dict(required=True),
            'name': dict(required=True),
            'values': dict(required=True),
            'state': dict(default='present', choices=['present', 'absent', 'exact']),
            'server_uri': dict(default='ldapi:///'),
            'start_tls': dict(default='false', choices=BOOLEANS),
            'bind_dn': dict(default=None),
            'bind_pw': dict(default=''),
        },
        supports_check_mode=True,
    )

    try:
        LdapAttr(module).main()
    except ldap.LDAPError, e:
        module.fail_json(msg=str(e), exc=format_exc())


class LdapAttr(object):
    def __init__(self, module):
        self.module = module

        # python-ldap doesn't understand unicode strings. Parameters that are
        # just going to get passed to python-ldap APIs are stored as utf-8.
        self.dn = self._utf8_param('dn')
        self.name = self._utf8_param('name')
        self.values = self._normalized_values()
        self.state = self.module.params['state']
        self.server_uri = self.module.params['server_uri']
        self.start_tls = self.module.boolean(self.module.params['start_tls'])
        self.bind_dn = self._utf8_param('bind_dn')
        self.bind_pw = self._utf8_param('bind_pw')

        self._connection = None

    def _utf8_param(self, name):
        return self._force_utf8(self.module.params[name])

    def _normalized_values(self):
        """ Parses the value parameter into a list of utf-8 strings. """
        values = self.module.params['values']

        if isinstance(values, basestring):
            if values == '':
                values = []
            else:
                values = [values]

        if not (isinstance(values, list) and all(isinstance(value, basestring) for value in values)):
            self.module.fail_json(msg="values must be a string or list of strings.")

        return map(self._force_utf8, values)

    def _force_utf8(self, value):
        """ If value is unicode, encode to utf-8. """
        if isinstance(value, unicode):
            value = value.encode('utf-8')

        return value

    def main(self):
        if self.state == 'present':
            modlist = self.handle_present()
        elif self.state == 'absent':
            modlist = self.handle_absent()
        elif self.state == 'exact':
            modlist = self.handle_exact()
        else:
            modlist = []

        if len(modlist) > 0:
            changed = True
            if not self.module.check_mode:
                self.connection.modify_s(self.dn, modlist)
        else:
            changed = False

        self.module.exit_json(changed=changed, modlist=modlist)

    #
    # State Implementations
    #

    def handle_present(self):
        values_to_add = filter(self.is_value_absent, self.values)
        if len(values_to_add) > 0:
            modlist = [(ldap.MOD_ADD, self.name, values_to_add)]
        else:
            modlist = []

        return modlist

    def handle_absent(self):
        values_to_delete = filter(self.is_value_present, self.values)
        if len(values_to_delete) > 0:
            modlist = [(ldap.MOD_DELETE, self.name, values_to_delete)]
        else:
            modlist = []

        return modlist

    def handle_exact(self):
        modlist = []

        current = self.current_values()
        if frozenset(self.values) != frozenset(current):
            if len(current) == 0:
                modlist = [(ldap.MOD_ADD, self.name, self.values)]
            elif len(self.values) == 0:
                modlist = [(ldap.MOD_DELETE, self.name, None)]
            else:
                modlist = [(ldap.MOD_REPLACE, self.name, self.values)]

        return modlist

    #
    # Util
    #

    def is_value_present(self, value):
        """ True if the target attribute has the given value. """
        try:
            is_present = bool(self.connection.compare_s(self.dn, self.name, value))
        except ldap.NO_SUCH_ATTRIBUTE:
            is_present = False

        return is_present

    def is_value_absent(self, value):
        """ True if the target attribute does not have the given value. """
        return (not self.is_value_present(value))

    def current_values(self):
        """ Returns the full list of values on the target attribute. """
        results = self.connection.search_s(self.dn, ldap.SCOPE_BASE, attrlist=[self.name])
        values = results[0][1].get(self.name, [])

        return values

    #
    # LDAP Connection
    #

    @property
    def connection(self):
        """ An authenticated connection to the LDAP server (cached). """
        if self._connection is None:
            self._connection = self._connect_to_ldap()

        return self._connection

    def _connect_to_ldap(self):
        connection = ldap.initialize(self.server_uri)

        if self.start_tls:
            connection.start_tls_s()

        if self.bind_dn is not None:
            connection.simple_bind_s(self.bind_dn, self.bind_pw)
        else:
            connection.sasl_interactive_bind_s('', ldap.sasl.external())

        return connection


from ansible.module_utils.basic import *  # noqa
main()

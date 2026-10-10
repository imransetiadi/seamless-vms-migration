#!/usr/bin/env bash
# vendor-openstack-cloud.sh - copy the openstack.cloud modules the os-migrate roles call as
# os_migrate.os_migrate.<module> into a collection tree (SDD §6, §17.1).
#
#   ansible-galaxy collection install openstack.cloud:2.6.0 -p /usr/share/ansible/collections
#   scripts/vendor-openstack-cloud.sh <collection root> /usr/share/ansible/collections/ansible_collections/openstack/cloud/plugins
#
# The developer checkout uses `make vendor-links` (git submodule + symlinks); the image build
# cannot rely on a submodule, so it installs the pinned release from Galaxy and copies the
# files with the same module_utils rewrite the Makefile applies. Keep VENDORED_MODULES and
# VENDORED_MODULE_UTILS identical to the Makefile (tests/unit/test_role_modules_resolve.py
# checks that every module the roles reference exists in the tree or in this list).
set -euo pipefail
VENDORED_MODULES="auth compute_flavor compute_flavor_info floating_ip identity_domain identity_role
identity_user identity_user_info image image_info keypair network networks_info port project
project_info role_assignment router security_group security_group_rule server
server_action server_info server_volume subnet subnets_info volume volume_info"
VENDORED_MODULE_UTILS="openstack ironic"

root=${1:?collection root}
src=${2:?openstack.cloud plugins directory}
[ -d "$src/modules" ] && [ -d "$src/module_utils" ] || { echo "no openstack.cloud plugins under $src" >&2; exit 1; }
for mod in $VENDORED_MODULES; do
  cp "$src/modules/$mod.py" "$root/plugins/modules/$mod.py"
  sed -i.bak 's/ansible_collections\.openstack\.cloud\.plugins\.module_utils\.openstack/ansible_collections.os_migrate.os_migrate.plugins.module_utils.openstack/g' \
    "$root/plugins/modules/$mod.py" && rm -f "$root/plugins/modules/$mod.py.bak"
done
for util in $VENDORED_MODULE_UTILS; do
  cp "$src/module_utils/$util.py" "$root/plugins/module_utils/$util.py"
done
echo "vendored $(echo $VENDORED_MODULES | wc -w | tr -d ' ') openstack.cloud modules into $root"

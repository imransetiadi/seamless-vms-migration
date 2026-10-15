.. Modified by the Seamless Migrate project, 2026.

Releasing the OS Migrate collection
===================================

Set env vars with old and new versions:

::

   OLD_VERSION=$(yq e '.version' galaxy.yml)
   echo "OLD_VERSION=$OLD_VERSION"

   NEW_VERSION="SOME.NEW.VERSION"

Edit the version in galaxy.yml, in plugins/module_utils/const.py (the
version resource files carry, which ``tests/unit/test_version.py`` keeps
equal to galaxy.yml's) and any hardcoded values in functional tests:

.. code:: bash

   sed -i -e "s/^version: $OLD_VERSION/version: $NEW_VERSION/" .//galaxy.yml
   sed -i -e "s/^OS_MIGRATE_VERSION = \"$OLD_VERSION\"/OS_MIGRATE_VERSION = \"$NEW_VERSION\"/" plugins/module_utils/const.py
   grep -lr "os_migrate_version: $OLD_VERSION" ./tests | xargs sed -i -e "s/^os_migrate_version: $OLD_VERSION/os_migrate_version: $NEW_VERSION/"

Create a pull request with these changes. Once it's merged, check out
the merge commit and release to galaxy. ``scripts/publish.sh`` builds the
tarball of the checked-out commit itself, even when an earlier build left
one, and reads the Galaxy API key from the ``ANSIBLE_GALAXY_API_KEY``
environment variable. ``read -rs`` keeps the key off the command line and
out of the shell history:

::

   git checkout $MERGED_COMMIT
   read -rs ANSIBLE_GALAXY_API_KEY && export ANSIBLE_GALAXY_API_KEY
   ./scripts/publish.sh

The script runs on the host: it needs ``ansible-galaxy``, ``curl``, ``jq``
and ``yq`` there, and runs ``make build``, whose build runs in the
``os-migrate`` container. ``-k <YOUR_GALAXY_TOKEN>`` still works, but puts
the key where ``ps`` and the shell history show it.

After a successful release, create a tag on the commit you built the
release from, and push the tag to the upstream repo:

::

   git tag -m "$NEW_VERSION" "$NEW_VERSION"
   # assuming the os-migrate upstream repo is named 'upstream' in your repo clone
   git push upstream --tags

If you've incremented "X" or "Y" in "X.Y.Z" version scheme, create also
a stable branch to allow us to create ".Z" releases:

::

   STABLE_VERSION=$(awk -F. '{ print $1 "." $2; }' <<<"$NEW_VERSION")
   git checkout -b stable/$STABLE_VERSION
   git push upstream stable/$STABLE_VERSION

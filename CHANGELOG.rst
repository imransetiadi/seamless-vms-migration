.. Modified by the Seamless Migrate project, 2026.

Changelog
=========

This project follows a simple changelog to satisfy collection metadata validation.

1.0.2
-----

- Maintenance updates for CI, linting, and sanity checks.

1.0.3
-----

- Change openstacksdk version in AEE to 4.5.0 to stop github CI from failing

1.0.4
-----

- Remove mention to OCP in the readme
- Revert make file change for submodule
- Add pylint, create bindep in root repo and remove useless dep in galaxy.yml
- Remove community.general dependency
- Cert checks
- update checksum to usedforsecurity=false

1.0.5
-----

- Add documentation for stringFilter module
- Add galaxy ignore file and fix links in the README
- Move import on top the module

1.1.0
-----

- Warm migration of OpenStack workloads (Seamless Migrate, to RHOSO 18.0): the
  disks of a running server are copied in snapshot pre-copy passes and only the
  final chunk delta is transferred after it is stopped.
- Add ``blocksync`` (``plugins/module_utils/blocksync.py``), a stdlib-only
  BLAKE2b chunk delta-sync engine with a verified manifest digest per pass, and
  ``tests/perf/bench_blocksync.py``.
- Add the modules ``import_workload_warm_snapshot``,
  ``import_workload_warm_sync`` and ``import_workload_rollback``, the role
  ``import_workloads_warm`` and the playbooks ``import_workloads_precopy.yml``,
  ``import_workloads_cutover.yml`` and ``rollback_workloads.yml``.
- ``import_workload_create_instance`` can record the new server in the warm
  state (``warm_state_dir``) and then never creates it twice.
- ``import_from_hypervisor`` exports disks read-only, bound to
  ``os_migrate_nbdkit_bind_address`` (default ``127.0.0.1``) with
  ``--shared=1``, quotes paths, keeps logs and PID files in a private directory
  and stops a previous export only through its PID file. With
  ``os_migrate_nbdkit_protocol: tcp``, set ``os_migrate_nbdkit_bind_address``
  to the hypervisor's migration-network IP (the role refuses the loopback
  default); exports started by 1.0.5 must be stopped by hand once.
- The cold path's nbdkit export of source volumes is read-only.
- ``blocksync`` recognises all-zero chunks with a byte count before hashing them
  (about 2.5× faster on never-written regions of thin volumes; chunks with data
  are unaffected).
- The source of the conversion hosts' security group rules is configurable
  (``os_migrate_conversion_secgroup_remote_ip_prefix``).
- ``os_migrate_workloads_preserve_volume_type`` (default ``false``) makes the
  cold path and ``import_workload_warm_sync`` create destination volumes with
  the serialized ``volume_type`` instead of the destination default type.
- Progress files are replaced atomically; destination volume parameters are
  shared by the cold and warm paths.
- Remove the unused ``sshpass`` from bindep.
- Resource files are now written with ``os_migrate_version: 1.1.0``: re-export
  data exported with 1.0.5.
- Warm migration (``import_workloads_warm``, ``blocksync``): ``assume_zero``
  is never applied to a final pass; a rollback with
  ``os_migrate_rollback_delete_dest_volumes`` keeps a destination volume that
  is still attached to a server other than the destination conversion host
  (``kept_volume_ids``, new ``conversion_host`` option of
  ``import_workload_rollback``); a cutover re-run fails instead of skipping a
  recorded destination server that is in ``ERROR`` or gone; recorded
  destination volumes without a source device are reported.
- ``import_workload_warm_sync`` reuses a recorded destination volume safely: a
  bootable one is marked bootable again (a pass interrupted between the create
  and the flag left it non-bootable, and every cutover then failed after the
  source was stopped), one still ``creating`` is waited for, and one in
  ``error`` is deleted and created again.
- ``import_workload_prelim`` names the progress and log files after the VM
  through ``filesystem.workload_file_stem`` (``%``, ``/`` and NUL written
  ``%25``, ``%2F``, ``%00``): a VM name such as ``../x`` or ``/etc/x`` no
  longer makes the modules write outside ``workload_logs``. Other names keep
  their file names.
- Attach locks on the conversion hosts name their owner as
  ``{migrator hostname}:{pid}``; before it waits, a module removes a lock whose
  owner ran on its own host and is gone (a module killed by a cancel or a
  timeout), instead of treating every owner as alive. A lock of another host,
  or one written by an earlier version, is left alone.
- ``import_workload_prelim`` treats the VM name as a literal when it looks for
  a destination server of the same name (``re.escape``, then only an equal
  name counts): ``web.01`` was skipped as already migrated because of an
  unrelated ``web-01``, and a name such as ``app(`` failed.
- The conversion-host SSH runs with ``ServerAliveInterval=15`` and
  ``ServerAliveCountMax=4``: a silent network partition fails a pass after
  about a minute instead of keeping the source stopped for about two hours.
- A warm cutover re-run finishes the recorded destination server instead of
  only reporting it: ``import_workload_create_instance`` waits for it to be
  ``ACTIVE`` and adds the floating IPs it lacks (a first attempt may have
  failed after Nova created the server). Floating IP creation keeps one
  already on the server's port for that fixed address, never a second.
- ``port_creation_mode: neutron`` recreates each port with its MAC, every fixed
  IP and its security groups. The export records each port's
  ``security_group_refs`` and ``is_port_security_enabled``, and the port is
  created with them and with the description ``os-migrate:{source port id}``,
  never with ``binding_profile`` (admin-only, host-specific). A re-run uses
  again the unbound port an earlier attempt created, and refuses any other port
  with the MAC or a port exported without its security groups (Nova applies a
  server's security groups only to the ports it creates). The default ``nova``
  mode is unchanged, and data exported before stays valid.
  ``import_workload_rollback`` deletes those ports
  (``deleted_port_ids``), also when the cutover failed before the server
  existed. Nova keeps such ports when their server is deleted later.
- A fixed IP reference without ``subnet_ref`` names only its address: Nova
  gets it as ``fixed_ip`` and Neutron as ``{ip_address}``, and Neutron picks
  the subnet of the port's network that holds it. Seamless Migrate writes such
  references for a per-VM NIC override (a VM's port moved to another network
  or given other addresses). Exported references keep their ``subnet_ref``.
- ``import_workload_dst_check`` checks the ports and floating IPs before the
  source stops. It looks up what each port of the workload needs, the way the
  import will after the stop: the port's network and every fixed IP's subnet
  reference. A ``%auth%`` reference searches only the credential's own project,
  so a shared network that another project owns, or a name that two of the
  project's networks carry, is reported. Each fixed IP must also be one that
  Neutron's IPAM takes on the port:

  - a host address of its subnet, or of a subnet of the network when only the
    address is given (not an IPv4 network address, nor a broadcast address
    below a /31, nor an IPv6 subnet's first address for a port Nova creates);
  - on a SLAAC or DHCPv6-stateless subnet, only the EUI-64 address of the kept
    MAC with ``port_creation_mode: neutron``, and no explicit address through
    Nova, which gives the port a new MAC.

  The floating IP the import would attach or create is looked up too.
- ``import_workload_dst_check`` also refuses, before the source stops, an
  address of a port that another port of the destination network holds
  (Neutron's ``IpAddressAlreadyAllocated``), and with
  ``port_creation_mode: neutron`` another port with its MAC on the network.
  The unbound port an earlier attempt created is the one exception, since
  the import uses it again.
- ``import_workload_dst_check`` also refuses, before the source stops, a port
  on a network that another project owns where the credential may not put it.
  Such a network must be shared with the credential's project or external: an
  admin sees every project's networks, and a port on another tenant's private
  network would put the server there. The credential's token must also carry
  a role the destination's policy profile counts. Neutron's policy
  (``create_port:fixed_ips:ip_address``, ``create_port:mac_address``,
  ``create_port:port_security_enabled``) lets only those roles, or the
  network's own project, choose a port's address, MAC or port security:
  ``admin`` or ``advsvc`` under the legacy rules (2023.1), ``admin`` or
  ``service`` under the secure-RBAC defaults (from 2023.2), and ``admin``
  alone when the profile is unknown. That refusal advises checking the
  provider, so its series is detected, or declaring its policy profile or
  series: a check is allowed while a plan runs. The profile is the server's
  ``_migration_params.policy_profile``, which Seamless Migrate writes before
  each run; an export without it reads ``legacy``, and a value the check
  does not know counts ``admin`` alone. The import chooses them for every
  port: Nova creates the port with the user's token and its fixed IP, and
  ``port_creation_mode: neutron`` sends the MAC, every address and the
  port-security flag.
- A port recreated through Neutron is created without its addresses on SLAAC
  or DHCPv6-stateless subnets. Neutron 2023.1 allocates such a subnet's EUI-64
  address of the MAC on every port it creates, also when the address is sent,
  and a second allocation of one address fails (``IpAddressAlreadyAllocated``)
  after the source has stopped. With the MAC kept Neutron derives the same
  address, which ``import_workload_dst_check`` makes sure of. An explicit,
  possibly empty, list keeps Neutron from adding an address of any other
  subnet. Using again the port an earlier attempt created no longer counts the
  addresses Neutron added on those subnets.
- An IPv6 subnet's first address is no longer refused for a port Nova creates:
  Nova creates its ports with no owner, and Neutron's ``check_subnet_ip`` takes
  that address for a port without one.
- ``import_workload_dst_check`` refuses a port without exactly one fixed IP
  when the ports are recreated through Nova (``port_creation_mode: nova``).
  Nova creates each port with one address, and the import refused any other
  count only after the source had stopped. A port whose addresses changed
  after validation is now refused while the source still runs.
- ``import_workload_dst_check`` looks up a port's own security groups when the
  ports are recreated through Neutron (``port_creation_mode: neutron``), as
  ``create_or_update`` will. A group the destination lacks, or has several
  of, used to fail the import only after the source had stopped.
- ``import_workload_dst_check`` refuses an external network that is not shared,
  the credential's own included, unless its token has the ``admin`` role. Nova
  attaches such a network to a server, as a requested network or through a
  port, only for admins (2023.1 ``network:attach_external_network``), and
  ``advsvc`` does not count; the import failed after the source had stopped.
- With ``port_creation_mode: neutron``, a re-run uses again an earlier
  attempt's unbound port only when it is in the credential's project. An admin
  credential sees every project's ports, and Nova attaches no port of another
  project to a server (``PortNotUsable``). Such a port with the MAC is now
  refused, by ``import_workload_dst_check`` before the source stops and by
  ``create_or_update`` before Nova is asked for the server. Before, the import
  failed only after the source had stopped.
- ``import_workload_dst_check`` refuses, before the source stops, a port
  exported without ``security_group_refs`` when the ports are recreated
  through Neutron (``port_creation_mode: neutron``). Such an export predates
  that field. ``create_or_update`` refuses the port rather than give it the
  project's default group, and that refusal came only after the source had
  stopped. Export the workload again. A port with an empty list, such as one
  with port security off, is not affected.
- ``import_workload_dst_check`` takes ``network_as_warnings`` (default
  ``false``), and the warm pre-copy role sets it before each pre-copy or sync
  pass. A pass makes no port or floating IP, so their problems are now
  warnings in its log, which say the cutover will refuse them, and only the
  server's own prerequisites (flavor, image, security groups, key pair) fail
  it. Before, such a problem failed a pass that may copy a disk for hours,
  although it could be fixed while the copy ran. The cutover's check still
  refuses it before the source stops.
- ``import_from_hypervisor`` never lets qemu guess a disk's format. It takes
  each disk's format from the instance's ``disk.info``, where Nova records it,
  and passes it to ``qemu-img info -f`` and ``qemu-nbd --format``. Before, the
  format came from ``qemu-img info`` without ``-f``: a raw disk whose first
  block the guest had written as a qcow2 header was exported as qcow2, and
  ``qemu-nbd``, run with sudo, followed the backing file that header named,
  reading a file of the hypervisor into the destination volume. The role stops
  before any export when ``disk.info`` is missing or unreadable, or records no
  single plain format for a disk. It no longer exports the config drive
  (``disk.config``): Nova gives the destination server its own. Disks are
  picked by file name only: an instances path containing ``disk``, any
  character, then ``info`` no longer hides every disk.
- The cold import (``import_workloads``) records its workload for a rollback,
  in the state file the warm path keeps: each volume its transfer creates, as
  soon as Cinder created it and before its data is copied
  (``import_workload_transfer_volumes`` with the new option
  ``warm_state_dir``), so a transfer that fails or is cut short leaves the
  record of every volume it created, and the server, as soon as Nova accepts
  it (``import_workload_create_instance`` with ``warm_state_dir``). A rollback with
  ``os_migrate_rollback_delete_dest_volumes`` then deletes exactly those
  volumes and keeps any other volume attached to the server. Before, a cold
  rollback found the server by name and left its data volumes behind,
  unrecorded: a cold retry creates new volumes, so they were never used or
  deleted again. A workload imported before keeps the old behaviour.
- ``rollback_workloads.yml`` takes ``os_migrate_rollback_start_source``
  (default ``true``): ``false`` leaves the source server as it is. Seamless
  Migrate uses it before a cold cutover of a migration an earlier warm
  pre-copy left data for, to remove those volumes and the warm state without
  starting a source a retry may keep stopped.
- ``import_workload_rollback`` waits for a destination volume Cinder is
  already deleting, such as the boot volume Nova deletes together with its
  server (``delete_on_termination``), instead of deleting it a second time,
  which Cinder refuses: a cold rollback no longer fails before it starts the
  source.
- ``import_workload_rollback`` takes ``delete_ports`` (default ``true``), and
  ``rollback_workloads.yml`` ``os_migrate_rollback_delete_ports``: ``false``
  deletes no destination port. Seamless Migrate's cleanup after a cancel
  uses it, as it made no port and an unbound one keyed to the VM's source
  port may belong to another migration of the same VM.
- ``import_workload_rollback`` with ``match_by_name`` looks a server up by
  name only when the workload has no state file at all; a state file that
  records no server means Nova never accepted one, so a same-named server
  is another one and is kept.
- A volume transfer that fails before its port forwarding starts (no free
  port on the destination conversion host, an SSH error) reports its own
  error. Before, its teardown read the PID of a forwarding process that did
  not exist, so it ended in ``AttributeError`` and skipped releasing the
  ports it had claimed, which then stayed claimed on the conversion host.
- A rollback with ``os_migrate_rollback_delete_dest_volumes`` empties the
  workload's warm state record instead of removing the file
  (``import_workload_rollback`` returns ``state_cleared`` in place of
  ``state_deleted``). The file is the sign that the workload's migration
  recorded it: a later rollback with ``os_migrate_rollback_match_by_name``
  then never takes a same-named server, such as another workload's, for the
  workload's. Before, a cleanup before a re-run removed the file and the next
  rollback matched any server by its name.
- ``import_workload_rollback`` finds the destination server Nova accepted
  while its answer was lost by the recorded volumes the server holds, when
  the workload's record names no server or one Nova no longer has (an
  operator deleted the recorded server and retried). The single server named
  like the workload that holds one of them, attached through Nova and not to
  the destination conversion host, is the workload's and is deleted. Before,
  it kept running beside the restarted source. A same-named server holding
  none of them is kept, also with ``os_migrate_rollback_match_by_name``, and
  two such servers are an error.
- The volume transfers create each destination volume without waiting, hand
  it to the record (the cold import's ``warm_state_dir``) as soon as Cinder
  accepted it, then wait for it and set its bootable flag. A volume Cinder
  puts in ``error`` is recorded too, and a rollback deletes it with its
  quota. A volume whose record cannot be written is deleted once Cinder
  settled it, before the transfer fails. Before, a failure while the SDK
  waited for the volume lost its id.
- A cold re-run's transfer removes the volume an earlier attempt recorded for
  each device before it creates any volume: the cold path never reuses a
  volume, so an earlier attempt's data volume, left detached when its server
  was deleted, no longer drops out of the record unremoved. A volume only the
  destination conversion host holds (a transfer cut short) is detached from
  it, under the host's attach lock, and deleted. A volume another server
  holds (a server renamed rather than deleted) is that server's and only
  leaves the record. Each deleted volume is waited for until Cinder no longer
  has it, within the transfer's timeout; one Cinder is already deleting, such
  as the boot volume Nova deletes with its server, is not deleted a second
  time, and one that began going while the transfer deleted it - Cinder
  refusing that delete - is waited for the same way. A volume the transfer
  cannot detach or delete otherwise, one that leaves
  ``deleting`` without going, or one still there at the timeout, fails the
  transfer before it creates a volume, with the record unchanged. Before, a
  re-run failed on every retry, with the source stopped, when it sent a
  second delete to such a boot volume, and a stop during the removal could
  leave a volume it had just created unrecorded.
- A volume Cinder no longer has reads as gone with the openstacksdk 4.x the
  collection runs on, whose ``get_volume_by_id`` raises ``NotFoundException``
  for a missing volume instead of returning ``None`` as its docstring says.
  Before, every check for a gone volume raised instead. A cold rollback then
  failed once Nova had deleted the boot volume with its server, before
  starting the source; a warm pass failed on a recorded volume that was
  gone instead of creating a new one; and a cold re-run failed on an
  earlier attempt's boot volume. The unit tests' fake cloud now raises the
  same way.
- The collection artifact leaves out the Seamless Migrate control plane,
  dashboard and deployment around the collection (``seamless/``,
  ``dashboard/``, ``deploy/``, the agent configuration and the product's
  ``RELEASING.md``), and everything the
  checkout's ``.gitignore`` files name, at the depths they name it.
  ``ansible-galaxy collection build`` packs files from disk rather than from
  git, so a tarball built at the repository root (``make build``,
  ``scripts/publish.sh``) could carry local secrets: the cloud credentials
  ``make generate-auth-files`` writes to ``tests/auth_tenant.yml``,
  ``clouds.yml`` files, ``secrets/``, ``.env`` files and the track checkouts
  under ``.worktrees/``. ``galaxy.yml``'s ``build_ignore`` now mirrors the
  ``.gitignore`` patterns, and a unit test derives a sample from every
  pattern. Never publish a tarball built before this change.
- Every detach of a volume from a server ends within the module's
  ``timeout``: the source VM's data volumes, the conversion hosts, the warm
  passes and the rollback. openstacksdk 4.21's ``detach_volume`` with
  ``wait=True`` ignores its ``timeout`` and waits without bound for the
  volume to be ``available``, so a detach that never ended hung the module,
  holding the conversion host's attach lock in a locked section. A detach
  now asks Nova without waiting, then polls the volume until it no longer
  lists that server's attachment and, unless another server still holds
  it, until Cinder shows it ``available``. A volume that goes to ``error``
  raises, and a detach still not done at the timeout fails with ``Timed out
  after N s waiting to detach volume V from server S``.
- A cold export that fails puts back what it took.
  ``import_workload_export_volumes`` fails with its volume map, which records
  each port as soon as it is claimed, so the role's rescue runs
  ``import_workload_src_cleanup``: it stops the transfer's NBD exports and
  frees those ports. Before it fails, the export attaches the data volumes
  it detached, and no server holds, back to the source VM through the API
  alone, as a conversion host still unreachable stops the cleanup, then
  deletes the root copy no server holds, and its snapshot or image, the same
  way; never the source VM's own boot volume. Before,
  the export failed without the map, nothing was put back, and the rollback
  started the source VM without the data volumes the export had detached. The
  source cleanup
  takes such a partial map: it detaches each volume this transfer attached to
  the conversion host - marked before the export asked Nova to attach it, once
  openstacksdk's own checks passed, whether or not its device was recorded or
  the attach's answer was lost; when the attach call fails, the export, still
  holding the attach lock, waits up to two minutes while Cinder shows the volume
  ``reserved`` or ``attaching`` and keeps the mark only if Cinder then lists the
  conversion
  host or the attach is still under way; never a shared multi-attach volume
  another migration's export attached - re-attaches the data volumes, and
  removes the
  root copy, its snapshot or the image only
  when the export made them, never the VM's own boot volume. The export
  creates the root snapshot, the root copy and the image without
  openstacksdk's own wait, which raises with the resource made, records
  each at once, then waits for it within the module's timeout: a snapshot
  or copy that went to ``error``, or an image still saving, was left
  behind, and a copy left so failed the rescue. The cleanup re-attaches the
  data volumes first and tries every step: a root copy Cinder will not
  delete no longer keeps the data volumes off the source VM, and the cleanup
  fails at the end with ``Source cleanup incomplete: ...`` naming each step
  it could not do. The failure's map carries no volume or VM names, which the
  cleanup never reads: Ansible prints the failed result, and Seamless Migrate
  retried a stuck detach of a volume whose name held "timeout". The source
  cleanup puts the volumes back before it frees the transfer's ports, logs a
  release that fails, and takes no port lock for a map that records no port:
  a port lock another migrator holds made it give up before the handback, and
  the rollback started the source VM without its data volumes. It frees them
  only if one ssh check then reaches the conversion host, as a failed transfer
  does: a host that went down during the handback had the release wait another
  15 minutes for its port lock, the source VM stopped meanwhile.
- The warm sync has blocksync check each device it opens. ``blocksync send``
  and ``receive`` take ``--expect-serial ID`` and fail with exit 3, before
  they read or write a byte, when the virtio serial of the device they opened
  names another volume. The warm sync passes the temporary volume's id to the
  sender and the destination volume's id to the receiver. The kernel gives a
  freed ``/dev/vdX`` path to the next disk attached, so a path another volume
  took after the attach was read, or written into. The check reads the opened
  descriptor's own device, so nothing can take the path in between. A device
  without a serial (a regular file, a disk not virtio-blk) is used as given.
- After an attach call that succeeded, the wait for the volume's device path,
  which holds the conversion host's attach lock, fails at once when Cinder
  shows the volume ``available`` again or in ``error`` with no attachment:
  nova-compute rolled the attach back. One still ``reserved`` or
  ``attaching`` gives up after ten minutes, ample for a slow Nova. The wait
  spent the module's whole timeout, 30 minutes by default, while every other
  migration on that host queued behind the lock. An attach rolled back so
  leaves no mark for the source cleanup. The transfer's attach to the
  destination conversion host waits the same way. This wait and the settle
  wait after a failed call measure their minutes, however slowly Cinder
  answers: they counted polls, each a Cinder GET as well as a second, so a
  loaded Cinder stretched the ten minutes to thirty.
- The cold export checks each disk's identity before it serves it, and the
  cold transfer each destination volume's before qemu-img or nbdcopy writes
  it. The check reads the disk's virtio serial, the volume id Nova gives it
  cut to 20 characters, and fails if the serial names another volume, or if a
  virtio-blk disk (``vdX``) no longer has a serial file, having left the
  conversion host; the NBD server, qemu-img or nbdcopy is given the disk's
  by-id link. The kernel gives a freed ``/dev/vdX`` path to the next disk
  attached, so a volume taken off the host could have the export serve
  another VM's disk, or the transfer write over one, without an error. A disk
  with no serial file (not virtio-blk) is used by its path, unchecked. ssh
  failing at the check of the by-id link gives up with ``SSH timeout: ...``,
  which Seamless Migrate retries, as it does ssh failing at the serial read:
  the error quoted no ssh command line, so one blip there failed the
  migration as permanent. A destination disk that fails the check fails the
  transfer by its own message, as the export reports its own, so Seamless
  Migrate fails it as permanent: the transfer let the error escape, and
  Ansible printed its cause, the serial read's ssh command line with its
  ConnectTimeout, which Seamless Migrate retried - two more whole cold copies
  with the source VM stopped. After nbdcopy exits, whatever its exit, the
  transfer checks that the path it wrote is still a block device: nbdcopy
  opens its destination again with ``O_CREAT`` when the first open fails, so
  a volume that left the host before that open had its disk copied into a
  regular file nbdcopy created in ``/dev``. Where ``/dev`` had room the
  transfer reported success with the volume never written; on RHEL 9's 4 MiB
  ``/dev`` nbdcopy failed with an error that named nothing and left the file.
  The regular file is removed - udev 252 (RHEL 9) will not make the volume's
  link over one - and the transfer fails, saying whether the file was
  removed, could not be removed (ssh failing at the removal is said as
  such) or could not be checked, and naming nbdcopy's exit when the volume
  left without a file. Ssh failing at the check gives up as ``SSH timeout:
  ...`` after a finished copy or one ssh broke; after nbdcopy failed on its
  own the transfer keeps that failure, permanent, and says the path could
  not be checked. A volume taken away after the export started is not
  caught: nbdkit resolves the path it serves, the disk's by-id link
  included, once, at its start.
- A wait for a conversion host's attach or port lock gives up once ssh to the
  host has failed for 15 minutes without a break, with ``Lock timeout:
  conversion host ADDRESS unreachable over SSH for N s, waiting for LOCK``,
  which Seamless Migrate retries as transient. It tried up to 1800 times, each
  try up to the 10 s connect timeout, about 5.5 hours, while a cold cutover's
  source stayed stopped. The window rides out a conversion host reboot or a
  network blip, and the step then completes. One failed connection, or a lock
  another migration holds, starts the window again. The source cleanup's own
  SSH check rides out the same window, then fails with ``SSH timeout:
  conversion host ADDRESS unreachable over SSH for N s, waiting for the source
  cleanup``: a reboot that starts outside a lock wait, mid-export or
  mid-transfer, left volumes on the conversion host that only the cleanup gives
  back, and a cleanup that failed at once let the rollback start the source VM
  without them. The other modules' checks still fail at once. The export's
  wait for its NBD exports, and the transfer's waits for the forwarded exports
  and an nbdkit socket, give up the same way after the window (``SSH timeout:
  ...``), where they tried ``timeout`` times, about 5.5 hours by default. So does
  the probe for a free port, which read ssh's 255 as a port in use and walked
  every ephemeral port, about 45 hours; it tries the same port again once a
  second while ssh fails, so an ssh that fails at once, a refused connection
  or a dropped route, ends in the same give-up, not in ``No free ports on
  conversion host!``. A failed transfer releases its ports only if one ssh
  check then reaches the conversion host, where a second wait for the port
  lock of a host it cannot reach held the source another 15 minutes and
  replaced its error; a release that fails is logged, never raised over the
  transfer's error; and one that claimed no port, its first wait for the port
  lock having given up, releases none rather than wait that lock again. A
  give-up reads ``Lock timeout`` or ``SSH timeout`` only: the wait that would
  give up with any other prefix, which Seamless Migrate would not retry, fails
  as it starts, and so does setting such a prefix later, or giving one past
  the setter: the give-up checks its prefix again as it is built.
- The warm state file (``workload_warm/{server}.json``) is synced to disk, its
  directory after the rename too, so the rename outlives a crash: a rollback
  reads there which destination volumes and server to delete. A filesystem
  that cannot sync a directory still gets the file.
- The roles' loops over workloads (``import_workloads``,
  ``import_workloads_warm``, ``export_workloads``, ``import_from_hypervisor``)
  label each item with its id only. Ansible printed the whole workload, or
  its name, on each include and result line, and Seamless Migrate classifies
  a failure as transient by the playbook output: a workload whose name or
  boot image properties (Nova's ``os_shutdown_timeout``) held "timeout" made
  a failure meant to be permanent, such as a stuck detach, be retried. The
  lines now read ``(item=<id>)``.
- A read of the conversion host's port map that fails under the port lock
  ends the transfer with its own error: ssh's failure as ``SSH timeout: ...``,
  the give-up Seamless Migrate retries, any other as ``Unable to read the port
  map``. It ended in a Python ``UnboundLocalError`` before.
- ``import_from_hypervisor`` checks the export port with ``ss`` (iproute2)
  instead of ``netstat``, which RHEL 9 compute nodes do not install by
  default: there every export timed out "waiting for port".
- ``os_migrate_nbdkit_timeout`` and ``os_migrate_nbdkit_ip_allow`` are gone
  from ``import_from_hypervisor``: nothing read them, so ``ip_allow`` promised
  a restriction the export never had. ``os_migrate_nbdkit_bind_address`` is
  the control over who can reach an export.
- ``rollback_workloads.yml`` with the role's catch-all workload filter
  (``regex: .*``, every workload of the data dir) is refused unless
  ``os_migrate_rollback_all: true``; an exact filter rolls back one workload.
- ``import_from_hypervisor`` sizes a destination volume from qemu-img's exact
  byte count, rounded up to GiB; it read the GiB figure with a regex and fell
  back to 0, which Cinder refuses.
- ``update_workload_nbdkit_uris`` takes typed ``nbdkit_disks`` suboptions, so
  ``port`` and ``size`` stay integers on ansible-core before 2.19 (where
  ``"{{ x | int }}"`` renders to a string).
- A warm pre-copy pass records what the cutover will refuse
  (``import_workload_dst_check``'s ``cutover_refusals``) beside the workload's
  warm state as ``<server id>.refusals.json`` (mode 0600), replaced on every
  pass and empty once the problems are fixed.
- ``conversion_host_content`` sets up a conversion host by its OS: RHEL
  with its subscription tasks; CentOS Stream 9 and 10, Rocky Linux 9 and
  AlmaLinux 9 with EPEL (before, only an ``ansible_distribution`` of CentOS
  ran these tasks, and a Rocky or AlmaLinux host installed nothing); Ubuntu
  24.04 LTS or later with apt and fresh package lists (``qemu-utils``,
  ``nbdkit``, ``libnbd-bin``, ``libguestfs-tools``). The defaults' variable
  ``os_migrate_conversion_host_content_tasks`` names the task file. Any
  other OS fails the role with a message naming it, where it used to install
  nothing and the transfer then failed for want of nbdkit. An Ubuntu cloud
  image logs in as ``ubuntu``, not ``cloud-user``.
- Each conversion host logs in as its own user:
  ``os_migrate_src_conversion_host_ssh_user`` and
  ``os_migrate_dst_conversion_host_ssh_user`` (both default to
  ``os_migrate_conversion_host_ssh_user``) reach the source and the
  destination host - the deployed hosts' inventory, the export, the source
  cleanup, the warm snapshot and rollback on one side, the transfer, the
  failure cleanup and the warm sync on the other. The transfer modules and
  ``import_workload_warm_sync`` take ``src_ssh_user`` (default ``ssh_user``)
  for the destination host's hop to the source host, so a RHEL source host
  (``cloud-user``) works with an Ubuntu destination host (``ubuntu``).
- Every stop and start of a source server waits first until Nova runs no stop
  or start of it: the warm cutover's stop, the cold import's stop
  (``os_migrate_workload_stop_before_migration``) and the rollback's start.
  Nova keeps a server ``ACTIVE``, with the task state ``powering-off``, until
  the guest is down - and ``SHUTOFF``, with ``powering-on``, until it runs -
  and refuses a stop or a start meanwhile, while ``server_action`` decides by
  the status alone. So a rollback after a stop that was cut off (a timeout, a
  503 during its wait) started nothing and the source ended ``SHUTOFF``, a
  re-run's stop was refused with 409, and a rollback retried while its first
  start still ran was refused with 409. The wait reads the server every
  ``os_migrate_source_stop_poll_s`` seconds (default 5), at most
  ``os_migrate_timeout``, and once more at the latest after
  ``os_migrate_timeout`` when the poll interval is longer: a stop or start
  that still runs then fails the play, where a wait of no retry used to start
  nothing and report success. Each role names it ``Wait until no stop or start
  of the source server runs``.
- A volume made from a server snapshot - the warm ``boot_disk_copy`` pass and
  the cold export of an image-booted server - is as large as Cinder needs for
  the image: the image file rounded up to whole GiB, its virtual disk (a qcow2
  snapshot holds a larger disk than its file) and its ``min_disk``, at least
  1 GiB. A file of 2.5 GiB with ``min_disk`` 0 (a flavor with a 0 GiB root
  disk) asked for 2 GiB, and Cinder refused it on every pass. Where Glance
  records no virtual size (before Victoria), a ``qcow2`` snapshot's is read
  from the file's own header, the first bytes of a streamed download.
- A warm pass whose record of a new destination volume cannot be written (a
  full disk) deletes that volume, once Cinder settled it, before it fails, as
  the cold transfer does. Before, no record named the volume, so no rollback
  ever deleted it and the guest's data stayed in the tenant. A volume an
  earlier pass extended to the source's grown size but could not record is
  recorded at the size Cinder reports, rather than extended again to the size
  it has, which Cinder refused on every later pass.
- A warm pass first detaches from the destination conversion host the
  recorded destination volumes a pass interrupted earlier left attached, then
  extends one whose source volume grew. Before, the extend ran first: where
  Cinder refuses to extend an ``in-use`` volume (its microversion or backend do
  not allow it), every later pass failed the same way until an operator
  detached the volume by hand; where it extends one, the pass waited until its
  timeout for the volume to read ``available``, which an attached volume never
  does.
- A warm cutover's re-run finishes the destination server its warm state
  records through ``import_workload_create_instance``'s new ``finish_only``
  option, with which the module never creates a server: a recorded server
  that vanished after the role looked it up fails the re-run, which names the
  rollback. Before, the module fell through to a create with an empty block
  device mapping - a server without the synchronised volumes, an image-booted
  VM booting from its image - and recorded it as the destination. Without
  ``finish_only`` (the first create, the cold import) a recorded server that
  is gone is still replaced by the one the call creates.
- A cold import whose transfer failed cleans the destination volumes the
  transfer created: ``import_workload_dst_failure_cleanup`` takes the
  workload's record (new ``warm_state_dir``; ``volume_map`` now optional) when
  the transfer returned no map, and the role's rescue runs it whenever the
  transfer ran. Before, the rescue skipped it when the transfer itself failed,
  and the volumes stayed attached to the destination conversion host. Its SSH
  check at the start no longer fails it: a conversion host it cannot reach
  keeps the volumes it holds for the rollback, and those no server holds are
  deleted through the API. The cleanup is best effort and never fails: a
  detach or a deletion that fails leaves that volume, logged and named in the
  result (``volumes_left``), and the cleanup goes on with the next one - but a
  wait that gives up ends its phase, as each later volume would wait it out
  again: a lock wait that gives up (the conversion host lost, or the lock
  another conversion holds throughout, now ``LockUnavailable``) or a detach
  Nova does not end in time (``DetachTimeout``) ends the detaches, and a
  deletion Cinder does not finish in time ends the deletions; one that cannot
  start (its conversion host gone, a record not the workload's) says so in its
  result. The rescue runs it before the source cleanup, so a source cleanup
  that fails no longer keeps it from running, and a failed transfer is
  reported and classified by its own failure, not the cleanup's.

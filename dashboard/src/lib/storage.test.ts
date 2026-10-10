import { describe, expect, it } from 'vitest';
import type { VMRef } from '../api/types';
import { handoverTargets, resolveDestination, splitHost, storageBackends, storageSummary, volumeTypeFamilies } from './storage';

const SRC = storageBackends({
  storage_backends: [
    { pool: 'overcloud@tripleo_ceph#ssd', vendor: 'Open Source', protocol: 'ceph', family: 'rbd' },
    { pool: 'overcloud@tripleo_netapp#192.0.2.50:/cinder', vendor: 'NetApp', protocol: 'nfs', family: 'netapp_nfs' },
  ],
});
const DST = storageBackends({
  storage_backends: [
    { pool: 'hostgroup@ceph#ssd', vendor: 'Open Source', protocol: 'ceph', family: 'rbd' },
    { pool: 'hostgroup@ceph#hdd', vendor: 'Open Source', protocol: 'ceph', family: 'rbd' },
    { pool: 'hostgroup@ontap-nfs#198.51.100.50:/cinder', vendor: 'NetApp', protocol: 'nfs', family: 'netapp_nfs' },
    { pool: 'hostgroup@ontap-nfs#198.51.100.50:/gold', vendor: 'NetApp', protocol: 'nfs', family: 'netapp_nfs' },
    { pool: 'hostgroup@ontap-iscsi#flex_a', vendor: 'NetApp', protocol: 'iSCSI', family: 'netapp_block' },
  ],
});

describe('storage backends (SDD §7.3.1, §10)', () => {
  it('reads the capability defensively and splits Cinder hosts', () => {
    expect(storageBackends({})).toEqual([]);
    expect(storageBackends({ storage_backends: 'nope' })).toEqual([]);
    expect(storageBackends({ storage_backends: [{ pool: 'x' }, null] })).toEqual([{ pool: 'x', vendor: null, protocol: null, family: 'other' }]);
    expect(splitHost('hostgroup@ontap-nfs#198.51.100.50:/cinder')).toEqual(['hostgroup@ontap-nfs', '198.51.100.50:/cinder']);
    expect(splitHost('hostgroup@ceph')).toEqual(['hostgroup@ceph', null]);
  });

  it('summarises the pools per family', () => {
    expect(storageSummary(DST)).toBe('Ceph RBD (2 pools), NetApp ONTAP NFS (2 pools), NetApp ONTAP iSCSI/FC (1 pool)');
  });

  it('offers NetApp backends (pool resolved per volume) and RBD pools as handover targets', () => {
    expect(handoverTargets('netapp_nfs', DST)).toEqual(['hostgroup@ontap-nfs']);
    expect(handoverTargets('netapp_block', DST)).toEqual(['hostgroup@ontap-iscsi']);
    expect(handoverTargets('rbd', DST)).toEqual(['hostgroup@ceph#hdd', 'hostgroup@ceph#ssd']);
    expect(handoverTargets('other', DST)).toEqual([]);
    // unknown family: every supported target
    expect(handoverTargets(null, DST)).toEqual(['hostgroup@ceph#hdd', 'hostgroup@ceph#ssd', 'hostgroup@ontap-iscsi', 'hostgroup@ontap-nfs']);
  });

  it('finds the family of every volume type of the selected VMs', () => {
    const vm = (disks: Array<Partial<VMRef['disks'][number]>>) => ({ disks }) as unknown as VMRef;
    const families = volumeTypeFamilies(
      [
        vm([{ kind: 'volume', volume_type: 'ceph-ssd', pool: 'overcloud@tripleo_ceph#ssd' }]),
        vm([
          { kind: 'volume', volume_type: 'netapp-nfs', pool: 'overcloud@tripleo_netapp#192.0.2.50:/cinder' },
          { kind: 'volume', volume_type: 'legacy', pool: null },
          { kind: 'image_root', volume_type: null, pool: null },
        ]),
      ],
      SRC,
    );
    expect([...families]).toEqual([
      ['ceph-ssd', 'rbd'],
      ['legacy', null],
      ['netapp-nfs', 'netapp_nfs'],
    ]);
  });

  it('resolves the destination pool like the executor (SDD §7.3.1)', () => {
    expect(resolveDestination('netapp_nfs', '192.0.2.50:/cinder', 'hostgroup@ontap-nfs', DST)).toEqual({ host: 'hostgroup@ontap-nfs#198.51.100.50:/cinder' });
    expect(resolveDestination('netapp_block', 'flex_a', 'hostgroup@ontap-iscsi', DST)).toEqual({ host: 'hostgroup@ontap-iscsi#flex_a' });
    expect(resolveDestination('rbd', 'x', 'hostgroup@ceph#ssd', DST)).toEqual({ host: 'hostgroup@ceph#ssd' });
    expect(resolveDestination('netapp_nfs', '192.0.2.50:/other', 'hostgroup@ontap-nfs', DST).error).toMatch(/no pool for the export \/other/);
    expect(resolveDestination('netapp_nfs', '192.0.2.50:/cinder', 'hostgroup@ontap-iscsi', DST).error).toMatch(/same driver family/);
    expect(resolveDestination('other', null, 'hostgroup@ceph#ssd', DST).error).toMatch(/unsupported storage family/);
    expect(resolveDestination('rbd', 'x', 'hostgroup@ceph', DST).error).toMatch(/name the pool/);
    // SDD §7.3.1: a named RBD pool the destination does not list, or a non-RBD backend, is refused up front
    expect(resolveDestination('rbd', 'x', 'hostgroup@ceph#typo', DST).error).toMatch(/lists no pool hostgroup@ceph#typo/);
    expect(resolveDestination('rbd', 'x', 'hostgroup@other#p', []).error).toMatch(/lists no pools for hostgroup@other/);
    expect(resolveDestination('rbd', 'x', 'hostgroup@ontap-iscsi', DST).error).toMatch(/netapp_block/);
  });
});

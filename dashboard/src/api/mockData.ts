/**
 * Deterministic fixture data for the in-browser mock adapter (VITE_SEAMLESS_MOCK=1).
 * Covers every Phase, Strategy, PlanStatus, provider status, finding Severity and advisor note
 * kind/source so the whole UI can be exercised without the control plane.
 * Estimates follow SDD §9.1, eligibility §9.2 and findings a subset of §9.3.
 */
import type {
  AdvisorNote,
  DestinationInventory,
  Disk,
  Estimate,
  Event,
  Finding,
  Migration,
  Nic,
  Phase,
  PhaseChange,
  Plan,
  Provider,
  ProviderKind,
  Strategy,
  SyncPass,
  VMRef,
} from './types';
import { guestOsOf, identifyGuestOs } from '../lib/guestOs';
import { resolveDestination, splitHost, storageBackends } from '../lib/storage';

export const GiB = 1024 ** 3;
export const MiB = 1024 ** 2;

export function iso(ms: number): string {
  return new Date(Math.round(ms / 1000) * 1000).toISOString().replace('.000Z', 'Z');
}

/** Small deterministic PRNG (mulberry32). */
export function prng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// -------------------------------------------------------------------------------------------
// VMs
// -------------------------------------------------------------------------------------------

type DiskSpec = Partial<Disk> & { size_gb: number };
interface VmSpec {
  id: string;
  name: string;
  project?: string;
  flavor?: string;
  vcpus?: number;
  ram_mb?: number;
  disks: DiskSpec[];
  nics?: Partial<Nic>[];
  power_state?: VMRef['power_state'];
  os_type?: string;
  host?: string;
  tags?: Record<string, string>;
  flavor_extra_specs?: Record<string, string>;
  cbt_enabled?: boolean | null;
  snapshot_count?: number;
  tools_ok?: boolean | null;
  change_rate_mibps?: number;
}

/** Cinder pool of each mock volume type (SDD §4.2 `Disk.pool`): Ceph RBD and an ONTAP NFS export. */
const MOCK_POOLS: Record<string, string> = {
  'tripleo-ceph': 'overcloud@tripleo_ceph#tripleo-ceph',
  'tripleo-ceph-ssd': 'overcloud@tripleo_ceph#tripleo-ceph-ssd',
  'ceph-ssd': 'overcloud@tripleo_ceph#ceph-ssd',
  'ceph-hdd': 'overcloud@tripleo_ceph#ceph-hdd',
  'netapp-nfs': 'overcloud@tripleo_netapp#192.0.2.60:/cinder_dc1',
  lvm: 'lab@lvm#lvm',
};

const backend = (pool: string, family: 'rbd' | 'netapp_nfs' | 'other') => ({
  pool,
  vendor: family === 'netapp_nfs' ? 'NetApp' : 'Open Source',
  protocol: family === 'netapp_nfs' ? 'nfs' : family === 'rbd' ? 'ceph' : 'iSCSI',
  family,
});

const DC1_STORAGE = [
  backend('overcloud@tripleo_ceph#ceph-hdd', 'rbd'),
  backend('overcloud@tripleo_ceph#ceph-ssd', 'rbd'),
  backend('overcloud@tripleo_ceph#tripleo-ceph', 'rbd'),
  backend('overcloud@tripleo_ceph#tripleo-ceph-ssd', 'rbd'),
  backend('overcloud@tripleo_netapp#192.0.2.60:/cinder_dc1', 'netapp_nfs'),
];

const PROD_STORAGE = [
  backend('hostgroup@ceph-hdd#volumes-hdd', 'rbd'),
  backend('hostgroup@ceph-nvme#volumes-nvme', 'rbd'),
  backend('hostgroup@ceph-ssd#volumes-ssd', 'rbd'),
  // the same ONTAP export, mounted through the RHOSO storage network's LIF
  backend('hostgroup@ontap-nfs#198.51.100.60:/cinder_dc1', 'netapp_nfs'),
];

export function makeVm(spec: VmSpec): VMRef {
  const disks: Disk[] = spec.disks.map((d, i) => {
    const kind = d.kind ?? 'volume';
    const volumeType = d.volume_type === undefined ? 'tripleo-ceph' : d.volume_type;
    return {
    id: d.id ?? `${spec.id}-disk-${i}`,
    name: d.name ?? `${spec.name}-${i === 0 ? 'root' : `data${i}`}`,
    size_gb: d.size_gb,
    used_gb: d.used_gb === undefined ? null : d.used_gb,
    bootable: d.bootable ?? i === 0,
    volume_type: volumeType,
    device: d.device ?? `/dev/vd${String.fromCharCode(97 + i)}`,
    kind,
    multiattach: d.multiattach ?? false,
    encrypted: d.encrypted ?? false,
    independent: d.independent ?? false,
    pool: d.pool !== undefined ? d.pool : kind === 'volume' && volumeType ? (MOCK_POOLS[volumeType] ?? null) : null,
    };
  });
  const diskBytes = disks.reduce((sum, d) => sum + d.size_gb * GiB, 0);
  const everyUsed = disks.every((d) => d.used_gb !== null);
  const usedBytes = everyUsed
    ? Math.round(disks.reduce((sum, d) => sum + (d.used_gb ?? 0) * GiB, 0))
    : Math.floor(diskBytes * 0.6);
  return {
    source_id: spec.id,
    name: spec.name,
    project: spec.project ?? null,
    flavor: spec.flavor ?? null,
    vcpus: spec.vcpus ?? 2,
    ram_mb: spec.ram_mb ?? 4096,
    disks,
    nics: (spec.nics ?? [{ network: 'tenant-app' }]).map((n, i) => ({
      network: n.network ?? 'tenant-app',
      mac: n.mac ?? `fa:16:3e:${(i + 10).toString(16)}:${spec.id.slice(-4, -2)}:${spec.id.slice(-2)}`,
      fixed_ips: n.fixed_ips ?? [`10.20.${i + 1}.${(parseInt(spec.id.slice(-2), 16) % 200) + 10}`],
      vnic_type: n.vnic_type ?? 'normal',
      mtu: n.mtu === undefined ? 1450 : n.mtu,
    })),
    power_state: spec.power_state ?? 'running',
    os_type: spec.os_type ?? 'rhel9',
    host: spec.host ?? null,
    tags: spec.tags ?? {},
    flavor_extra_specs: spec.flavor_extra_specs ?? {},
    cbt_enabled: spec.cbt_enabled === undefined ? null : spec.cbt_enabled,
    snapshot_count: spec.snapshot_count ?? 0,
    tools_ok: spec.tools_ok === undefined ? null : spec.tools_ok,
    change_rate_bps: spec.change_rate_mibps === undefined ? null : spec.change_rate_mibps * MiB,
    disk_bytes: diskBytes,
    used_bytes: usedBytes,
    guest_os: identifyGuestOs(spec.os_type ?? 'rhel9'),
  };
}

const OS_VMS: VmSpec[] = [
  { id: 'os-0a11', name: 'web-01', project: 'shop', flavor: 'm1.medium', vcpus: 4, ram_mb: 8192, disks: [{ size_gb: 40, used_gb: 12 }], nics: [{ network: 'tenant-web' }], tags: { app: 'shop', tier: 'web' }, host: 'compute-01' },
  { id: 'os-0a12', name: 'web-02', project: 'shop', flavor: 'm1.medium', vcpus: 4, ram_mb: 8192, os_type: 'ubuntu 22.04', disks: [{ size_gb: 40, used_gb: 14 }], nics: [{ network: 'tenant-web' }], tags: { app: 'shop', tier: 'web' }, host: 'compute-02' },
  { id: 'os-0a13', name: 'web-03', project: 'shop', flavor: 'm1.medium', vcpus: 4, ram_mb: 8192, disks: [{ size_gb: 40, used_gb: 13 }], nics: [{ network: 'tenant-web' }], tags: { app: 'shop', tier: 'web' }, host: 'compute-03' },
  { id: 'os-0a14', name: 'web-cache-01', project: 'shop', flavor: 'm1.small', disks: [{ size_gb: 30, used_gb: 9 }], nics: [{ network: 'tenant-web' }], tags: { app: 'shop', tier: 'cache' } },
  { id: 'os-0b21', name: 'api-gw-01', project: 'shop', flavor: 'm1.medium', vcpus: 4, ram_mb: 8192, os_type: 'rocky 9.4', disks: [{ size_gb: 60, used_gb: 21 }], tags: { app: 'gateway' } },
  { id: 'os-0b22', name: 'api-gw-02', project: 'shop', flavor: 'm1.medium', vcpus: 4, ram_mb: 8192, os_type: 'debian 12', disks: [{ size_gb: 60, used_gb: 20 }], tags: { app: 'gateway' } },
  { id: 'os-0b31', name: 'mq-broker-01', project: 'platform', flavor: 'm1.large', vcpus: 8, ram_mb: 16384, disks: [{ size_gb: 40, used_gb: 15 }, { size_gb: 100, used_gb: 46, volume_type: 'tripleo-ceph-ssd' }], tags: { app: 'rabbitmq' } },
  { id: 'os-0b32', name: 'mq-broker-02', project: 'platform', flavor: 'm1.large', vcpus: 8, ram_mb: 16384, disks: [{ size_gb: 40, used_gb: 15 }, { size_gb: 100, used_gb: 51, volume_type: 'tripleo-ceph-ssd' }], tags: { app: 'rabbitmq' } },
  { id: 'os-0c41', name: 'ad-dc-01', project: 'platform', flavor: 'm1.large', vcpus: 4, ram_mb: 16384, os_type: 'windows2019', disks: [{ size_gb: 80, used_gb: 38 }], nics: [{ network: 'tenant-infra', mtu: 1500 }], tags: { app: 'active-directory' } },
  { id: 'os-0d51', name: 'db-pg-01', project: 'finance', flavor: 'db.xlarge', vcpus: 16, ram_mb: 65536, disks: [{ size_gb: 50, used_gb: 18 }, { size_gb: 600, used_gb: 410, volume_type: 'tripleo-ceph-ssd', encrypted: true }], nics: [{ network: 'tenant-db' }], tags: { app: 'ledger', tier: 'database' }, change_rate_mibps: 8 },
  { id: 'os-0d52', name: 'db-mysql-02', project: 'shop', flavor: 'db.xlarge', vcpus: 16, ram_mb: 65536, disks: [{ size_gb: 50, used_gb: 16 }, { size_gb: 520, used_gb: 300, volume_type: 'tripleo-ceph-ssd' }], nics: [{ network: 'tenant-db' }], tags: { app: 'shop', tier: 'database' }, change_rate_mibps: 5 },
  { id: 'os-0d53', name: 'db-oracle-03', project: 'finance', flavor: 'db.xlarge', vcpus: 16, ram_mb: 131072, disks: [{ size_gb: 100, used_gb: 40 }, { size_gb: 1200, used_gb: 900, volume_type: 'tripleo-ceph-ssd' }], nics: [{ network: 'tenant-db' }], tags: { app: 'erp-finance', tier: 'database' }, change_rate_mibps: 12 },
  { id: 'os-0e61', name: 'legacy-rhel6-app', project: 'finance', flavor: 'm1.medium', os_type: 'rhel6', disks: [{ size_gb: 80, used_gb: 33 }], tags: { app: 'payroll' } },
  { id: 'os-0e62', name: 'gpu-render-01', project: 'media', flavor: 'g1.a10', vcpus: 8, ram_mb: 32768, disks: [{ size_gb: 200, used_gb: 120 }], flavor_extra_specs: { 'resources:VGPU': '1' }, tags: { app: 'render' } },
  { id: 'os-0e63', name: 'shared-disk-node-a', project: 'platform', flavor: 'm1.large', vcpus: 8, ram_mb: 16384, disks: [{ size_gb: 40, used_gb: 12 }, { size_gb: 300, used_gb: 140, multiattach: true }], tags: { app: 'gfs-cluster' } },
  { id: 'os-0e64', name: 'app-billing-01', project: 'finance', flavor: 'm1.large', vcpus: 8, ram_mb: 16384, disks: [{ size_gb: 50, used_gb: 22 }, { size_gb: 150, used_gb: 88 }], tags: { app: 'billing' }, change_rate_mibps: 3 },
  { id: 'os-0e65', name: 'static-cdn-01', project: 'shop', flavor: 'm1.small', disks: [{ size_gb: 20, used_gb: 6, kind: 'image_root', volume_type: null }], nics: [{ network: 'tenant-web' }], tags: { app: 'shop', tier: 'static' } },
  { id: 'os-0e66', name: 'report-gen-01', project: 'finance', flavor: 'm1.medium', os_type: 'windows 2022', disks: [{ size_gb: 60, used_gb: 25, volume_type: 'netapp-nfs' }], tags: { app: 'reporting' } },
  { id: 'os-0e67', name: 'old-ftp-01', project: 'platform', flavor: 'm1.small', power_state: 'stopped', disks: [{ size_gb: 30, used_gb: 4, volume_type: 'netapp-nfs' }], tags: { app: 'ftp' } },
  { id: 'os-0f71', name: 'analytics-node-1', project: 'analytics', flavor: 'm1.large', vcpus: 8, ram_mb: 32768, disks: [{ size_gb: 40, used_gb: 14, volume_type: 'ceph-ssd' }, { size_gb: 500, used_gb: 380, volume_type: 'ceph-ssd' }], nics: [{ network: 'analytics' }], tags: { app: 'spark' } },
  { id: 'os-0f72', name: 'analytics-node-2', project: 'analytics', flavor: 'm1.large', vcpus: 8, ram_mb: 32768, disks: [{ size_gb: 40, used_gb: 14, volume_type: 'ceph-ssd' }, { size_gb: 500, used_gb: 362, volume_type: 'ceph-ssd' }], nics: [{ network: 'analytics' }], tags: { app: 'spark' } },
  { id: 'os-0f73', name: 'analytics-node-3', project: 'analytics', flavor: 'm1.large', vcpus: 8, ram_mb: 32768, disks: [{ size_gb: 40, used_gb: 15, volume_type: 'ceph-ssd' }, { size_gb: 500, used_gb: 371, volume_type: 'ceph-ssd' }], nics: [{ network: 'analytics' }], tags: { app: 'spark' } },
  { id: 'os-0f74', name: 'analytics-db', project: 'analytics', flavor: 'db.xlarge', vcpus: 16, ram_mb: 65536, disks: [{ size_gb: 50, used_gb: 17, volume_type: 'ceph-ssd' }, { size_gb: 400, used_gb: 260, volume_type: 'ceph-hdd' }], nics: [{ network: 'analytics' }], tags: { app: 'spark', tier: 'database' }, change_rate_mibps: 6 },
  { id: 'os-1a01', name: 'jump-host-01', project: 'platform', flavor: 'm1.small', os_type: 'ubuntu 18.04', disks: [{ size_gb: 20, used_gb: 5 }], nics: [{ network: 'provider-ext', mtu: 1500 }], tags: { app: 'bastion' } },
  { id: 'os-1a02', name: 'build-runner-07', project: 'platform', flavor: 'm1.large', power_state: 'error', disks: [{ size_gb: 100 }], tags: { app: 'ci' } },
];

const VMW_VMS: VmSpec[] = [
  { id: 'vm-5001', name: 'vm-erp-app-01', project: 'erp', vcpus: 8, ram_mb: 32768, os_type: 'rhel8', cbt_enabled: true, tools_ok: true, disks: [{ size_gb: 120, used_gb: 64, kind: 'vmdk', volume_type: null, device: 'scsi0:0' }], nics: [{ network: 'VM Network ERP', mtu: 1500 }], tags: { app: 'erp' }, change_rate_mibps: 4 },
  { id: 'vm-5002', name: 'vm-erp-db-01', project: 'erp', vcpus: 16, ram_mb: 131072, os_type: 'rhel8', cbt_enabled: true, tools_ok: true, disks: [{ size_gb: 100, used_gb: 30, kind: 'vmdk', volume_type: null, device: 'scsi0:0' }, { size_gb: 800, used_gb: 590, kind: 'vmdk', volume_type: null, device: 'scsi0:1' }], nics: [{ network: 'VM Network ERP', mtu: 1500 }], tags: { app: 'erp', tier: 'database' }, change_rate_mibps: 10 },
  { id: 'vm-5003', name: 'vm-fileserver-01', project: 'erp', vcpus: 4, ram_mb: 16384, os_type: 'windows2019', cbt_enabled: false, tools_ok: true, disks: [{ size_gb: 100, used_gb: 40, kind: 'vmdk', volume_type: null, device: 'scsi0:0' }, { size_gb: 1500, used_gb: 1100, kind: 'vmdk', volume_type: null, device: 'scsi0:1' }], nics: [{ network: 'VM Network ERP', mtu: 1500 }], tags: { app: 'files' } },
  { id: 'vm-5004', name: 'vm-print-01', project: 'erp', vcpus: 2, ram_mb: 4096, os_type: 'windows2016', cbt_enabled: true, tools_ok: false, disks: [{ size_gb: 60, used_gb: 22, kind: 'vmdk', volume_type: null, device: 'scsi0:0' }, { size_gb: 20, used_gb: 3, kind: 'vmdk', volume_type: null, device: 'scsi0:1', independent: true }], nics: [{ network: 'VM Network ERP', mtu: 1500 }], tags: { app: 'print' } },
  { id: 'vm-5005', name: 'vm-hr-portal', project: 'erp', vcpus: 4, ram_mb: 8192, os_type: 'Ubuntu 24.04.1 LTS', cbt_enabled: true, tools_ok: true, snapshot_count: 2, disks: [{ size_gb: 80, used_gb: 31, kind: 'vmdk', volume_type: null, device: 'scsi0:0' }], nics: [{ network: 'VM Network ERP', mtu: 1500 }], tags: { app: 'hr' } },
  { id: 'vm-5006', name: 'vm-legacy-win2008', project: 'erp', vcpus: 2, ram_mb: 4096, os_type: 'windows2008', cbt_enabled: false, tools_ok: false, disks: [{ size_gb: 60, used_gb: 41, kind: 'vmdk', volume_type: null, device: 'scsi0:0' }], nics: [{ network: 'VM Network ERP', mtu: 1500 }], tags: { app: 'legacy-crm' } },
];

const LAB_VMS: VmSpec[] = [
  { id: 'lab-2b01', name: 'lab-k8s-master', project: 'lab', flavor: 'm1.large', vcpus: 4, ram_mb: 16384, disks: [{ size_gb: 80, used_gb: 25, volume_type: 'lvm' }], tags: { app: 'k8s-lab' } },
  { id: 'lab-2b02', name: 'lab-k8s-worker-1', project: 'lab', flavor: 'm1.large', vcpus: 8, ram_mb: 32768, disks: [{ size_gb: 120, used_gb: 48, volume_type: 'lvm' }], tags: { app: 'k8s-lab' } },
];

// -------------------------------------------------------------------------------------------
// Providers
// -------------------------------------------------------------------------------------------

function conversionHost(name: string, address: string) {
  return { manage: true, name, flavor: 'm1.large', external_network: 'provider-ext', image: 'rhel-9.4-conversion', ssh_user: 'cloud-user', address, ssh_allowed_cidr: null, ssh_key_secret: null };
}

export function buildProviders(now: number): Provider[] {
  return [
    {
      id: 'rhosp17-dc1',
      name: 'RHOSP 17.1 — DC1',
      kind: 'openstack',
      role: 'source',
      endpoint: 'https://overcloud.dc1.example.com:13000/v3',
      cloud: null,
      credentials_secret: 'provider-rhosp17-dc1',
      region: 'regionOne',
      verify_tls: true,
      ca_cert_path: '/etc/pki/seamless/dc1-ca.pem',
      conversion_host: conversionHost('seamless-conv-src', '192.0.2.21'),
      capabilities: { admin: true, compute_microversion: '2.79', ovn: false, volume_backends: DC1_STORAGE.map((b) => b.pool), storage_backends: DC1_STORAGE },
      status: 'ok',
      status_message: 'Keystone, Nova, Cinder, Neutron and Glance reachable.',
      last_checked_at: iso(now - 4 * 60_000),
      distribution: 'rhosp',
      credentials_updated_at: iso(now - 3 * 3600_000),
      conversion_key_updated_at: null,
    },
    {
      id: 'vcenter-hq',
      name: 'vCenter HQ',
      kind: 'vmware',
      role: 'source',
      endpoint: 'https://vcenter.hq.example.com/sdk',
      cloud: null,
      credentials_secret: 'provider-vcenter-hq',
      region: 'Datacenter-HQ',
      verify_tls: true,
      ca_cert_path: null,
      conversion_host: { manage: false, name: 'conv-vddk-hq', flavor: null, external_network: null, image: null, ssh_user: 'cloud-user', address: '198.51.100.40', ssh_allowed_cidr: null, ssh_key_secret: 'provider-vcenter-hq-ssh' },
      capabilities: { version: '8.0.2', cbt: true, datastores: ['vsanDatastore', 'nfs-archive'] },
      status: 'degraded',
      status_message: 'Two ESXi hosts in maintenance mode; CBT queries are slow.',
      last_checked_at: iso(now - 11 * 60_000),
      distribution: 'vmware',
      credentials_updated_at: iso(now - 26 * 3600_000),
      conversion_key_updated_at: iso(now - 26 * 3600_000),
    },
    {
      id: 'community-lab',
      name: 'OpenStack Lab (2023.1)',
      kind: 'openstack',
      role: 'source',
      endpoint: 'https://lab-openstack.example.com:5000/v3',
      cloud: 'community-lab',
      credentials_secret: null,
      region: 'RegionOne',
      verify_tls: false,
      ca_cert_path: null,
      conversion_host: null,
      capabilities: {},
      status: 'error',
      status_message: 'Keystone returned HTTP 503 Service Unavailable.',
      last_checked_at: iso(now - 26 * 60_000),
      distribution: 'openstack_community',
      credentials_updated_at: null,
      conversion_key_updated_at: null,
    },
    {
      id: 'kolla-edge',
      name: 'Kolla Edge (Bobcat)',
      kind: 'openstack',
      role: 'source',
      endpoint: 'https://kolla-vip.edge.example.com:5000/v3',
      cloud: null,
      credentials_secret: null,
      region: 'RegionOne',
      verify_tls: true,
      ca_cert_path: '/etc/pki/seamless/kolla-root.crt',
      conversion_host: null,
      capabilities: {},
      status: 'unknown',
      status_message: null,
      last_checked_at: null,
      distribution: 'kolla',
      credentials_updated_at: null,
      conversion_key_updated_at: null,
    },
    {
      id: 'rhoso-prod',
      name: 'RHOSO 18.0 — Production',
      kind: 'rhoso',
      role: 'destination',
      endpoint: 'https://keystone-public-openstack.apps.ocp.example.com/v3',
      cloud: 'rhoso-prod',
      credentials_secret: null,
      region: 'regionOne',
      verify_tls: true,
      ca_cert_path: '/etc/pki/seamless/ocp-ingress-ca.pem',
      conversion_host: conversionHost('seamless-conv-dst', '198.51.100.31'),
      capabilities: {
        admin: true,
        compute_microversion: '2.95',
        ovn: true,
        volume_backends: PROD_STORAGE.map((b) => b.pool),
        storage_backends: PROD_STORAGE,
      },
      status: 'ok',
      status_message: 'All services healthy; OVN networking.',
      last_checked_at: iso(now - 3 * 60_000),
      distribution: 'rhoso',
      credentials_updated_at: null,
      conversion_key_updated_at: null,
    },
    {
      id: 'rhoso-staging',
      name: 'RHOSO 18.0 — Staging',
      kind: 'rhoso',
      role: 'destination',
      endpoint: 'https://keystone-public-openstack.apps.ocp-stg.example.com/v3',
      cloud: 'rhoso-staging',
      credentials_secret: null,
      region: 'regionOne',
      verify_tls: true,
      ca_cert_path: null,
      conversion_host: null,
      capabilities: {},
      status: 'unknown',
      status_message: null,
      last_checked_at: null,
      distribution: 'rhoso',
      credentials_updated_at: null,
      conversion_key_updated_at: null,
    },
  ];
}

export function buildInventories(): Record<string, VMRef[] | DestinationInventory> {
  const destination: DestinationInventory = {
    networks: { 'tenant-web': 1442, 'tenant-app': 1442, 'tenant-db': 1442, 'tenant-infra': 1442, analytics: 1442, 'erp-net': 1442, 'provider-ext': 1500 },
    flavors: [
      { name: 'm1.small', vcpus: 2, ram_mb: 4096, disk_gb: 40, extra_specs: {} },
      { name: 'm1.medium', vcpus: 4, ram_mb: 8192, disk_gb: 80, extra_specs: {} },
      { name: 'm1.large', vcpus: 8, ram_mb: 32768, disk_gb: 160, extra_specs: {} },
      { name: 'db.xlarge', vcpus: 16, ram_mb: 131072, disk_gb: 200, extra_specs: { 'hw:cpu_policy': 'dedicated' } },
    ],
    volume_types: ['ceph-ssd', 'ceph-hdd', 'ceph-nvme', 'netapp-nfs'],
    quotas: {
      shop: { cores: 120, ram_mb: 262144, instances: 40, volumes: 80, gigabytes: 8000 },
      finance: { cores: 96, ram_mb: 393216, instances: 20, volumes: 40, gigabytes: 6000 },
      platform: { cores: 64, ram_mb: 131072, instances: 30, volumes: 60, gigabytes: 4000 },
      analytics: { cores: 128, ram_mb: 524288, instances: 16, volumes: 32, gigabytes: 12000 },
      erp: { cores: 64, ram_mb: 262144, instances: 20, volumes: 40, gigabytes: 9000 },
    },
    projects: ['shop', 'finance', 'platform', 'analytics', 'erp', 'media'],
  };
  return {
    'rhosp17-dc1': OS_VMS.map(makeVm),
    'vcenter-hq': VMW_VMS.map(makeVm),
    'community-lab': LAB_VMS.map(makeVm),
    'rhoso-prod': destination,
    'rhoso-staging': { networks: {}, flavors: [], volume_types: [], quotas: {}, projects: [] },
  };
}

// -------------------------------------------------------------------------------------------
// Estimator (SDD §9.1) and eligibility (§9.2)
// -------------------------------------------------------------------------------------------

const PARAMS = {
  scan_bps: 524288000,
  change_rate_bps: 2097152,
  shutdown_s: 60,
  boot_s: 120,
  create_s: 60,
  snapshot_s: 30,
  handover_per_volume_s: 20,
  v2v_s: 300,
  v2v_inplace_s: 120,
};

export function estimate(vm: VMRef, strategy: Strategy, plan: Plan, reasons: string[]): Estimate {
  const D = vm.disk_bytes;
  const U = vm.used_bytes;
  const c = vm.change_rate_bps ?? PARAMS.change_rate_bps;
  const L = plan.link_bps;
  const S = PARAMS.scan_bps;
  const V = vm.disks.length;
  const threshold = plan.convergence_threshold_bytes;
  const maxPasses = plan.max_sync_passes;
  let precopy = 0;
  let passes = 0;
  let downtime = 0;
  let finalDelta = 0;

  switch (strategy) {
    case 'cold':
      downtime = PARAMS.shutdown_s + PARAMS.snapshot_s + U / L + PARAMS.create_s + PARAMS.boot_s;
      break;
    case 'warm': {
      let t = PARAMS.snapshot_s + Math.max(U / L, D / S);
      precopy = t;
      passes = 1;
      let delta = Math.min(D, c * t);
      while (delta > threshold && passes < maxPasses) {
        t = PARAMS.snapshot_s + Math.max(D / S, delta / L);
        precopy += t;
        passes += 1;
        delta = Math.min(D, c * t);
      }
      finalDelta = delta;
      downtime = PARAMS.shutdown_s + PARAMS.snapshot_s + Math.max(D / S, finalDelta / L) + PARAMS.create_s + PARAMS.boot_s;
      break;
    }
    case 'storage_handover':
      downtime = PARAMS.shutdown_s + V * PARAMS.handover_per_volume_s + PARAMS.create_s + PARAMS.boot_s;
      break;
    case 'vmware_cold':
      downtime = PARAMS.shutdown_s + U / L + PARAMS.v2v_s + PARAMS.create_s + PARAMS.boot_s;
      break;
    case 'vmware_warm': {
      let t = U / L;
      precopy = t;
      passes = 1;
      let delta = Math.min(D, c * t);
      while (delta > threshold && passes < maxPasses) {
        t = 10 + delta / L;
        precopy += t;
        passes += 1;
        delta = Math.min(D, c * t);
      }
      finalDelta = delta;
      downtime = PARAMS.shutdown_s + finalDelta / L + PARAMS.v2v_inplace_s + PARAMS.create_s + PARAMS.boot_s;
      break;
    }
  }
  const round = (n: number) => Math.round(n * 10) / 10;
  return {
    strategy,
    eligible: reasons.length === 0,
    reasons,
    precopy_s: round(precopy),
    passes,
    downtime_s: round(downtime),
    total_s: round(precopy + downtime),
    final_delta_bytes: Math.round(finalDelta),
    meets_slo: downtime <= plan.downtime_slo_s,
  };
}

/** Per-volume driver-family checks of a handover where the pools are known (SDD §9.2, §7.3.1). */
function storageReasons(vm: VMRef, plan: Plan, source: Provider, destination: Provider): string[] {
  const families = new Map(storageBackends(source.capabilities).map((b) => [b.pool, b.family]));
  const dst = storageBackends(destination.capabilities);
  const reasons: string[] = [];
  for (const disk of vm.disks) {
    if (disk.kind !== 'volume' || !disk.pool || !families.has(disk.pool)) continue;
    const family = families.get(disk.pool)!;
    const target = plan.handover.backend_map[disk.volume_type ?? ''];
    if (family !== 'other' && (!target || dst.length === 0)) continue;
    const { error } = resolveDestination(family, splitHost(disk.pool)[1], target ?? '', dst);
    if (error) reasons.push(`${disk.name ?? disk.id}: ${error}`);
  }
  return reasons;
}


export function eligibility(
  vm: VMRef,
  sourceKind: ProviderKind,
  plan: Plan,
  source: Provider,
  destination: Provider,
  findings: Finding[],
): Partial<Record<Strategy, string[]>> {
  const result: Partial<Record<Strategy, string[]>> = {};
  const blockers = findings.filter((f) => f.severity === 'blocker').map((f) => `Blocker finding ${f.code}`);
  const multiattach = vm.disks.some((d) => d.multiattach);
  if (sourceKind === 'vmware') {
    result.vmware_cold = vm.power_state === 'error' ? ['Source VM is in error state'] : [];
    const warm: string[] = [];
    if (vm.cbt_enabled !== true) warm.push('Changed Block Tracking is not enabled');
    if (vm.disks.some((d) => d.independent)) warm.push('VM has an independent disk');
    result.vmware_warm = warm;
  } else {
    const cold: string[] = [];
    if (vm.power_state === 'error') cold.push('Source VM is in error state');
    if (!source.conversion_host || !destination.conversion_host) cold.push('No conversion host configured on both clouds');
    result.cold = cold;
    result.warm = [...cold, ...(multiattach ? ['VM has a multi-attach volume'] : [])];
    const handover: string[] = [];
    if (!plan.handover.enabled) handover.push('Storage handover is not enabled for this plan');
    if (vm.disks.some((d) => d.kind !== 'volume')) handover.push('Not every disk is a Cinder volume');
    const unmapped = vm.disks.filter((d) => !d.volume_type || !(d.volume_type in plan.handover.backend_map));
    if (plan.handover.enabled && unmapped.length) handover.push('A volume type has no RHOSO backend mapping');
    if (multiattach) handover.push('VM has a multi-attach volume');
    handover.push(...storageReasons(vm, plan, source, destination));
    if (!source.capabilities.admin || !destination.capabilities.admin) handover.push('Admin access is required on both clouds');
    result.storage_handover = handover;
  }
  if (blockers.length) {
    for (const key of Object.keys(result) as Strategy[]) result[key] = [...(result[key] ?? []), ...blockers];
  }
  return result;
}

// -------------------------------------------------------------------------------------------
// Pre-flight findings (subset of SDD §9.3)
// -------------------------------------------------------------------------------------------

export function preflight(
  vm: VMRef,
  sourceKind: ProviderKind,
  plan: Plan,
  destinationNetworks: Record<string, number | null>,
  selectedNames: string[],
): Finding[] {
  const findings: Finding[] = [];
  const add = (code: string, severity: Finding['severity'], message: string, remediation: string | null, strategies: Strategy[] = []) =>
    findings.push({ code, severity, message, remediation, strategies });

  if (vm.power_state === 'error') add('SRC_VM_ERROR_STATE', 'blocker', `${vm.name} is in ERROR state at the source.`, 'Repair or reset the source instance, then re-validate.');
  if (vm.power_state === 'transitioning') add('SRC_VM_TRANSITIONAL_STATE', 'blocker', `${vm.name} has a Nova task in flight (resize, migration, rescue, rebuild or reboot).`, 'Wait until the VM is ACTIVE or SHUTOFF, then re-validate.');
  if (selectedNames.filter((n) => n === vm.name).length > 1)
    add('SRC_VM_DUPLICATE_NAME', 'blocker', `Another selected VM is also named ${vm.name}; os-migrate filters workloads by name.`, 'Rename one of the instances or split them into separate plans.');
  if (vm.disks.some((d) => d.multiattach))
    add('SRC_VM_MULTIATTACH', 'warning', 'A multi-attach volume is attached; warm and storage handover are not possible.', 'Detach the shared volume or migrate cold during a window.', ['warm', 'storage_handover']);
  if (vm.disks[0] && (vm.disks[0].kind === 'image_root' || vm.disks[0].kind === 'ephemeral'))
    add('SRC_VM_EPHEMERAL_ROOT', 'info', 'Root disk is image-backed; each warm pass snapshots the server to Glance.', 'Expect slower pre-copy passes (boot_disk_copy).', ['warm']);
  for (const nic of vm.nics) {
    const mapped = plan.mappings.networks[nic.network] ?? nic.network;
    if (sourceKind !== 'vmware' && !(mapped in destinationNetworks))
      add('MAP_NETWORK_MISSING', 'blocker', `Network ${nic.network} has no mapping and no same-named RHOSO network.`, 'Add a network mapping in the plan.');
    const dstMtu = destinationNetworks[mapped];
    if (nic.mtu && dstMtu && dstMtu < nic.mtu)
      add('NET_MTU_SHRINK', 'warning', `Destination MTU ${dstMtu} is smaller than the source NIC MTU ${nic.mtu} on ${nic.network}.`, 'Lower the guest MTU or enable jumbo frames on the OVN underlay.');
    if (['direct', 'direct-physical', 'macvtap'].includes(nic.vnic_type))
      add('NET_SRIOV_PORT', 'warning', `Port on ${nic.network} uses vnic_type ${nic.vnic_type}.`, 'Re-create SR-IOV ports on RHOSO manually.');
  }
  if ('pci_passthrough:alias' in vm.flavor_extra_specs)
    add('VM_PCI_PASSTHROUGH', 'blocker', 'Flavor requests PCI passthrough.', 'Provide an equivalent PCI alias on RHOSO compute nodes.');
  if ('resources:VGPU' in vm.flavor_extra_specs)
    add('VM_VGPU', 'blocker', 'Flavor requests a vGPU (resources:VGPU).', 'Configure mediated devices on RHOSO and map the flavor.');
  if (vm.disks.some((d) => d.encrypted))
    add('VOL_ENCRYPTED', 'warning', 'An encrypted volume is attached; its Barbican key must be re-created at RHOSO.', 'Export the secret and register it in the RHOSO Key Manager before cutover.');
  const guest = guestOsOf(vm);
  if (guest.lifecycle === 'legacy')
    add('GUEST_OS_LEGACY', 'warning', `Guest OS ${guest.label} is out of standard vendor support.`, 'It still migrates: test the application on RHOSO, check the virtio drivers and plan a longer verification.');
  if (guest.family === 'unknown')
    add('GUEST_OS_UNKNOWN', 'info', `The guest OS is not identified${vm.os_type ? ` (${vm.os_type})` : ''}.`, 'Set the os_distro and os_version image properties or run VMware Tools; verification uses the Linux profile meanwhile.');
  if (sourceKind === 'vmware' && (guest.v2v === 'tech_preview' || guest.v2v === 'unverified'))
    add('GUEST_CONVERSION_UNVERIFIED', 'warning', `Converting ${guest.label} with virt-v2v is ${guest.v2v === 'tech_preview' ? 'a Technology Preview' : 'not supported by Red Hat'}.`, 'Run a test conversion of a copy first.', ['vmware_cold', 'vmware_warm']);
  if (sourceKind === 'vmware' && guest.v2v === 'unsupported')
    add('GUEST_CONVERSION_UNSUPPORTED', 'warning', `virt-v2v on the RHEL 9 conversion host cannot prepare ${guest.label}.`, 'Install the virtio storage and network drivers from an older virtio-win release inside the guest and test the conversion, or migrate the VM another way.', ['vmware_cold', 'vmware_warm']);
  if (sourceKind === 'vmware') {
    if (vm.cbt_enabled === false)
      add('VMW_CBT_DISABLED', 'warning', 'Changed Block Tracking is disabled; only cold migration is possible.', 'Enable CBT (requires a power cycle) or accept a cold window.', ['vmware_warm']);
    if (vm.disks.some((d) => d.independent))
      add('VMW_INDEPENDENT_DISK', 'warning', 'An independent disk cannot be tracked by CBT.', 'Convert the disk to dependent mode or migrate cold.', ['vmware_warm']);
    if (vm.snapshot_count > 0)
      add('VMW_SNAPSHOTS_PRESENT', 'warning', `${vm.snapshot_count} VMware snapshots exist.`, 'Consolidate snapshots before migration.');
    if (vm.tools_ok === false)
      add('VMW_TOOLS_MISSING', 'info', 'VMware Tools are not running; graceful shutdown may time out.', 'Install or start VMware Tools.');
  }
  if (plan.handover.enabled && sourceKind !== 'vmware' && vm.disks.some((d) => !d.volume_type || !(d.volume_type in plan.handover.backend_map)))
    add('HANDOVER_BACKEND_UNMAPPED', 'info', 'Handover is enabled but a volume type has no RHOSO backend mapping.', 'Add the volume type to the handover backend map.', ['storage_handover']);
  return findings;
}

// -------------------------------------------------------------------------------------------
// Plans and migrations
// -------------------------------------------------------------------------------------------

export function defaultPlanFields(now: number) {
  return {
    description: null,
    mappings: { networks: {}, flavors: {}, volume_types: {}, projects: {} },
    default_strategy: 'auto' as const,
    strategy_overrides: {},
    selection_policy: 'min_downtime' as const,
    downtime_slo_s: 600,
    require_approval: true,
    auto_cutover: false,
    cutover_window: null,
    keep_warm_interval_s: 900,
    convergence_threshold_bytes: 1073741824,
    max_sync_passes: 5,
    estimator_overrides: {},
    link_bps: 131072000,
    handover: { enabled: false, backend_map: {} },
    verification: {
      tcp_ports: [22],
      windows_tcp_ports: [3389],
      probe_address: 'fixed' as const,
      console_success_patterns: ['login:', 'Cloud-init v\\. .* finished', 'Reached target .*Multi-User'],
      timeout_s: 600,
      auto_rollback: true,
      use_advisor: true,
    },
    prestage_resources: ['networks', 'subnets', 'routers', 'router_interfaces', 'security_groups', 'security_group_rules'],
    waves: [],
    created_at: iso(now),
    updated_at: iso(now),
  };
}

interface MigrationSpec {
  id: string;
  vm: string;
  strategy: Strategy;
  phase: Phase;
  wave?: string | null;
  /** Seconds ago the current phase was entered. */
  enteredAgo: number;
  progress?: number;
  passes?: Array<{ kind: SyncPass['kind']; changedGiB: number; durationS: number; open?: boolean }>;
  downtimeStartedAgo?: number | null;
  downtimeS?: number | null;
  approvals?: Array<{ actor: string; ago: number; comment?: string }>;
  cutoverRequested?: boolean;
  error?: string | null;
  reviewReason?: string | null;
  notes?: Array<Omit<AdvisorNote, 'created_at'> & { ago: number }>;
  attempts?: number;
  destination?: string | null;
  checkpoint?: string | null;
}

interface PlanSpec {
  id: string;
  name: string;
  description: string;
  source: string;
  destination: string;
  status: Plan['status'];
  createdAgo: number;
  overrides?: Partial<Plan>;
  waves: Array<{ id: string; name: string; vms: string[]; depends_on?: string[]; max_parallel?: number }>;
  migrations: MigrationSpec[];
}

const H = 3600;
const D = 86_400;

const PLAN_SPECS: PlanSpec[] = [
  {
    id: 'plan-4f2a9c1e',
    name: 'DC1 → RHOSO production rollout',
    description: 'RHOSP 17.1 DC1 tenants to RHOSO 18.0; warm pre-copy for databases, pilot first.',
    source: 'rhosp17-dc1',
    destination: 'rhoso-prod',
    status: 'running',
    createdAgo: 3 * D,
    overrides: {
      mappings: { networks: { 'tenant-infra': 'tenant-infra' }, flavors: { 'g1.a10': 'm1.large' }, volume_types: { 'tripleo-ceph': 'ceph-hdd', 'tripleo-ceph-ssd': 'ceph-ssd' }, projects: {} },
      // PRD G1: warm cutovers within 15 minutes.
      downtime_slo_s: 900,
    },
    waves: [
      { id: 'wave-1', name: 'Pilot — stateless web', vms: ['os-0a11', 'os-0a12', 'os-0a13'], max_parallel: 3 },
      { id: 'wave-2', name: 'Databases — warm pre-copy', vms: ['os-0d51', 'os-0d52', 'os-0e64', 'os-0c41'], max_parallel: 4 },
      { id: 'wave-3', name: 'Middleware', vms: ['os-0b22', 'os-0b32', 'os-0e62'], depends_on: ['wave-1', 'wave-2'], max_parallel: 5 },
    ],
    migrations: [
      {
        id: 'mig-3c1a0f9e21', vm: 'os-0a11', strategy: 'warm', phase: 'completed', wave: 'wave-1', enteredAgo: 2 * H, progress: 100,
        passes: [{ kind: 'full', changedGiB: 12, durationS: 128 }, { kind: 'delta', changedGiB: 0.42, durationS: 84 }, { kind: 'final', changedGiB: 0.05, durationS: 81 }],
        downtimeStartedAgo: 2 * H + 212, downtimeS: 212, approvals: [{ actor: 'sari', ago: 2 * H + 300, comment: 'Pilot approved in CAB-2291' }], cutoverRequested: true,
        destination: 'b7e2c0f4-1d2a-4c55-9a1e-6f4c2d8e9a01', checkpoint: 'verify',
        notes: [{ kind: 'classification', source: 'rules', summary: 'Classified as stateless_web: name matches ^web- and tag tier=web.', confidence: null, data: { tier: 'stateless_web' }, ago: 3 * D }],
      },
      {
        id: 'mig-3c1a0f9e22', vm: 'os-0a12', strategy: 'warm', phase: 'verifying', wave: 'wave-1', enteredAgo: 70, progress: 100,
        passes: [{ kind: 'full', changedGiB: 14, durationS: 131 }, { kind: 'delta', changedGiB: 0.51, durationS: 86 }, { kind: 'final', changedGiB: 0.06, durationS: 83 }],
        downtimeStartedAgo: 250, approvals: [{ actor: 'sari', ago: 400 }], cutoverRequested: true, destination: '2f9d6b1e-77c4-4f0e-8d3a-0c5b9e7a6d12', checkpoint: 'cutover',
        notes: [{ kind: 'verification', source: 'jev', summary: 'Both claims verified: the guest finished booting and no filesystem errors were reported.', confidence: 0.88, data: { results: [{ claim: 'The guest operating system finished booting', verdict: 'verified' }] }, ago: 30 }],
      },
      {
        id: 'mig-3c1a0f9e23', vm: 'os-0a13', strategy: 'warm', phase: 'cutover', wave: 'wave-1', enteredAgo: 95, progress: 62,
        passes: [{ kind: 'full', changedGiB: 13, durationS: 129 }, { kind: 'delta', changedGiB: 0.47, durationS: 85 }, { kind: 'final', changedGiB: 0.05, durationS: 0, open: true }],
        downtimeStartedAgo: 95, approvals: [{ actor: 'sari', ago: 180, comment: 'Inside change window' }], cutoverRequested: true, checkpoint: 'precopy',
      },
      {
        id: 'mig-5d7e2b4a10', vm: 'os-0d51', strategy: 'warm', phase: 'precopy', wave: 'wave-2', enteredAgo: 31 * 60, progress: 37,
        passes: [{ kind: 'full', changedGiB: 0, durationS: 0, open: true }], checkpoint: 'prestage',
        notes: [{ kind: 'strategy', source: 'jev', summary: 'Warm recommended over cold: estimated downtime 4m 12s vs 1h 01m; storage handover not eligible (no shared Ceph).', confidence: 0.67, data: { selected: 'warm', candidates: ['cold', 'warm'], probabilities: { warm: 0.67, cold: 0.33 } }, ago: 2 * H }],
      },
      {
        id: 'mig-5d7e2b4a11', vm: 'os-0d52', strategy: 'warm', phase: 'syncing', wave: 'wave-2', enteredAgo: 14 * 60, progress: 48,
        passes: [{ kind: 'full', changedGiB: 316, durationS: 2590 }, { kind: 'delta', changedGiB: 12.6, durationS: 1132 }, { kind: 'delta', changedGiB: 0, durationS: 0, open: true }],
        checkpoint: 'precopy',
      },
      {
        id: 'mig-5d7e2b4a12', vm: 'os-0e64', strategy: 'warm', phase: 'awaiting_cutover', wave: 'wave-2', enteredAgo: 22 * 60, progress: 100,
        passes: [{ kind: 'full', changedGiB: 110, durationS: 905 }, { kind: 'delta', changedGiB: 2.7, durationS: 412 }, { kind: 'delta', changedGiB: 0.71, durationS: 418 }],
        checkpoint: 'sync',
        notes: [{ kind: 'classification', source: 'jev', summary: 'Classified as stateful_database (margin 0.41): billing ledger writes continuously.', confidence: 0.79, data: { tier: 'stateful_database', decision: 'auto' }, ago: 3 * D }],
      },
      {
        id: 'mig-5d7e2b4a13', vm: 'os-0c41', strategy: 'cold', phase: 'ready', wave: 'wave-2', enteredAgo: 2 * H,
        checkpoint: null,
        notes: [{ kind: 'classification', source: 'rules', summary: 'Classified as infrastructure_service: os_type windows2019 and tag app=active-directory.', confidence: null, data: { tier: 'infrastructure_service' }, ago: 3 * D }],
      },
      { id: 'mig-7a9b1c3d40', vm: 'os-0b22', strategy: 'cold', phase: 'validating', wave: 'wave-3', enteredAgo: 6 },
      { id: 'mig-7a9b1c3d41', vm: 'os-0b32', strategy: 'warm', phase: 'pending', wave: 'wave-3', enteredAgo: 40 * 60 },
      { id: 'mig-7a9b1c3d42', vm: 'os-0e62', strategy: 'cold', phase: 'blocked', wave: 'wave-3', enteredAgo: 2 * H },
    ],
  },
  {
    id: 'plan-7b3e0d52',
    name: 'VMware exit — ERP',
    description: 'vCenter HQ ERP cluster; CBT-enabled VMs run warm through vmware-migration-kit.',
    source: 'vcenter-hq',
    destination: 'rhoso-prod',
    status: 'paused',
    createdAgo: 6 * D,
    overrides: { mappings: { networks: { 'VM Network ERP': 'erp-net' }, flavors: {}, volume_types: {}, projects: { erp: 'erp' } }, downtime_slo_s: 600 },
    waves: [
      { id: 'wave-1', name: 'ERP application tier', vms: ['vm-5001', 'vm-5005'], max_parallel: 2 },
      { id: 'wave-2', name: 'ERP data and files', vms: ['vm-5002', 'vm-5003', 'vm-5004'], depends_on: ['wave-1'], max_parallel: 2 },
    ],
    migrations: [
      {
        id: 'mig-9e4f6a8b50', vm: 'vm-5001', strategy: 'vmware_warm', phase: 'syncing', wave: 'wave-1', enteredAgo: 9 * 60, progress: 71,
        passes: [{ kind: 'full', changedGiB: 64, durationS: 520 }, { kind: 'delta', changedGiB: 0, durationS: 0, open: true }],
        checkpoint: 'precopy',
      },
      { id: 'mig-9e4f6a8b51', vm: 'vm-5005', strategy: 'vmware_warm', phase: 'ready', wave: 'wave-1', enteredAgo: 5 * H },
      {
        id: 'mig-9e4f6a8b52', vm: 'vm-5002', strategy: 'vmware_warm', phase: 'ready', wave: 'wave-2', enteredAgo: 5 * H,
        approvals: [{ actor: 'sari', ago: 4 * H, comment: 'Pre-approved for the Saturday window' }],
      },
      { id: 'mig-9e4f6a8b53', vm: 'vm-5003', strategy: 'vmware_cold', phase: 'ready', wave: 'wave-2', enteredAgo: 5 * H },
      { id: 'mig-9e4f6a8b54', vm: 'vm-5004', strategy: 'vmware_cold', phase: 'pending', wave: 'wave-2', enteredAgo: 5 * H },
    ],
  },
  {
    id: 'plan-c81d44a0',
    name: 'Shared Ceph handover — analytics',
    description: 'RHOSO uses the existing external Ceph cluster; Spark nodes are handed over by Cinder unmanage/manage.',
    source: 'rhosp17-dc1',
    destination: 'rhoso-prod',
    status: 'validated',
    createdAgo: 1 * D,
    overrides: {
      handover: { enabled: true, backend_map: { 'ceph-ssd': 'hostgroup@ceph-ssd#volumes-ssd' } },
      cutover_window: null,
      downtime_slo_s: 600,
    },
    waves: [{ id: 'wave-1', name: 'Spark cluster', vms: ['os-0f71', 'os-0f72', 'os-0f73', 'os-0f74'], max_parallel: 4 }],
    migrations: [
      {
        id: 'mig-b2c4d6e870', vm: 'os-0f71', strategy: 'storage_handover', phase: 'ready', wave: 'wave-1', enteredAgo: 3 * H,
        notes: [{ kind: 'strategy', source: 'jev', summary: 'Storage handover recommended: metadata-only cutover (≈ 4 min) regardless of the 540 GiB of disks.', confidence: 0.71, data: { selected: 'storage_handover', candidates: ['storage_handover', 'warm'], probabilities: { storage_handover: 0.71, warm: 0.29 } }, ago: 3 * H }],
      },
      { id: 'mig-b2c4d6e871', vm: 'os-0f72', strategy: 'storage_handover', phase: 'ready', wave: 'wave-1', enteredAgo: 3 * H },
      { id: 'mig-b2c4d6e872', vm: 'os-0f73', strategy: 'storage_handover', phase: 'ready', wave: 'wave-1', enteredAgo: 3 * H },
      { id: 'mig-b2c4d6e873', vm: 'os-0f74', strategy: 'warm', phase: 'ready', wave: 'wave-1', enteredAgo: 3 * H },
    ],
  },
  {
    id: 'plan-0e9f6a17',
    name: 'Community lab cleanup',
    description: 'Retire the 2023.1 lab cloud. Draft: the source provider is currently failing its health check.',
    source: 'community-lab',
    destination: 'rhoso-prod',
    status: 'draft',
    createdAgo: 2 * H,
    overrides: { require_approval: false },
    waves: [],
    migrations: [
      { id: 'mig-c3d5e7f980', vm: 'lab-2b01', strategy: 'cold', phase: 'pending', enteredAgo: 2 * H },
      { id: 'mig-c3d5e7f981', vm: 'lab-2b02', strategy: 'cold', phase: 'pending', enteredAgo: 2 * H },
    ],
  },
  {
    id: 'plan-2d5c8b93',
    name: 'Pilot — static content',
    description: 'First production pilot: static web content and cache.',
    source: 'rhosp17-dc1',
    destination: 'rhoso-prod',
    status: 'completed',
    createdAgo: 9 * D,
    overrides: { downtime_slo_s: 600 },
    waves: [{ id: 'wave-1', name: 'Pilot', vms: ['os-0e65', 'os-0a14'], max_parallel: 2 }],
    migrations: [
      {
        id: 'mig-d4e6f8a090', vm: 'os-0e65', strategy: 'cold', phase: 'finalized', wave: 'wave-1', enteredAgo: 7 * D, progress: 100,
        downtimeStartedAgo: 8 * D + 900, downtimeS: 418, approvals: [{ actor: 'sari', ago: 8 * D + 1200 }], cutoverRequested: true, destination: '0c4e8a2b-5f1d-4e93-b7a6-3d2f1e0c9b88', checkpoint: 'finalize',
      },
      {
        id: 'mig-d4e6f8a091', vm: 'os-0a14', strategy: 'warm', phase: 'completed', wave: 'wave-1', enteredAgo: 8 * D, progress: 100,
        passes: [{ kind: 'full', changedGiB: 9, durationS: 96 }, { kind: 'delta', changedGiB: 0.31, durationS: 63 }, { kind: 'final', changedGiB: 0.04, durationS: 61 }],
        downtimeStartedAgo: 8 * D + 188, downtimeS: 188, approvals: [{ actor: 'sari', ago: 8 * D + 600 }], cutoverRequested: true, destination: '9a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d', checkpoint: 'verify',
      },
    ],
  },
  {
    id: 'plan-95a7e3f1',
    name: 'Legacy apps',
    description: 'End-of-life workloads; cold migration with manual verification.',
    source: 'rhosp17-dc1',
    destination: 'rhoso-prod',
    status: 'failed',
    createdAgo: 2 * D,
    overrides: { verification: { tcp_ports: [22, 8080], windows_tcp_ports: [3389], probe_address: 'fixed', console_success_patterns: ['login:'], timeout_s: 600, auto_rollback: false, use_advisor: true }, downtime_slo_s: 900 },
    waves: [{ id: 'wave-1', name: 'Legacy', vms: ['os-0e61', 'os-0e63', 'os-0e66', 'os-0e67'], max_parallel: 2 }],
    migrations: [
      {
        id: 'mig-e5f7a9b1a0', vm: 'os-0e61', strategy: 'cold', phase: 'failed', wave: 'wave-1', enteredAgo: 33 * 60, progress: 100,
        downtimeStartedAgo: 41 * 60, approvals: [{ actor: 'sari', ago: 45 * 60 }], cutoverRequested: true, destination: '5e6f7a8b-9c0d-4e1f-a2b3-c4d5e6f7a8b9', checkpoint: 'cutover', attempts: 1,
        error: 'Verification failed: console pattern "login:" not matched within 600 s; tcp:22 connection refused.',
        reviewReason: 'Jev contradicted "The guest operating system finished booting" (console shows a kernel panic).',
        notes: [
          { kind: 'screen', source: 'jev', summary: 'Console excerpt blocked: it contained instructions aimed at an AI agent (injection probability 0.97); dropped from evidence.', confidence: 0.97, data: { action: 'block', injection: 0.97 }, ago: 34 * 60 },
          { kind: 'verification', source: 'jev', summary: 'Contradicted: "The guest operating system finished booting" — console shows "Kernel panic - not syncing: VFS: Unable to mount root fs".', confidence: 0.91, data: { verdict: 'contradicted', action: 'auto' }, ago: 33 * 60 },
          {
            kind: 'similar_incidents', source: 'memory', summary: '2 similar incidents: RHEL 6 guests lacked virtio drivers in the initramfs after cold import.', confidence: null,
            data: { hits: [
              { title: 'RHEL 6 kernel panic after os-migrate import', content: 'Rebuild the initramfs with virtio_blk and virtio_pci (dracut --add-drivers) before cutover; retry succeeded.', score: 0.83 },
              { title: 'Payroll VM unbootable on RHOSO (VFS mount)', content: 'Root device changed from /dev/vda to /dev/sda in fstab; switched to UUID mounts.', score: 0.71 },
            ] },
            ago: 32 * 60,
          },
        ],
      },
      {
        id: 'mig-e5f7a9b1a1', vm: 'os-0e63', strategy: 'cold', phase: 'rolling_back', wave: 'wave-1', enteredAgo: 4 * 60, progress: 40,
        downtimeStartedAgo: 19 * 60, approvals: [{ actor: 'sari', ago: 25 * 60 }], cutoverRequested: true, checkpoint: 'cutover',
        error: 'Import failed: volume attach timed out on the destination conversion host.',
      },
      {
        id: 'mig-e5f7a9b1a2', vm: 'os-0e66', strategy: 'cold', phase: 'rolled_back', wave: 'wave-1', enteredAgo: 3 * H, progress: 0,
        downtimeStartedAgo: 3 * H + 1260, downtimeS: 1260, approvals: [{ actor: 'sari', ago: 4 * H }], cutoverRequested: true, checkpoint: 'rollback', attempts: 1,
        error: 'Rolled back by bayu: tcp:8080 closed after boot; application config references the old database host.',
      },
      { id: 'mig-e5f7a9b1a3', vm: 'os-0e67', strategy: 'cold', phase: 'cancelled', wave: 'wave-1', enteredAgo: 1 * D },
    ],
  },
];

/** Phase walk used to synthesize history for a migration that is currently in `phase`. */
function walkTo(strategy: Strategy, phase: Phase): Phase[] {
  const warm = strategy === 'warm' || strategy === 'vmware_warm';
  const success: Phase[] = warm
    ? ['pending', 'validating', 'ready', 'precopy', 'syncing', 'awaiting_cutover', 'cutover', 'verifying', 'completed', 'finalized']
    : ['pending', 'validating', 'ready', 'cutover', 'verifying', 'completed', 'finalized'];
  switch (phase) {
    case 'blocked':
      return ['pending', 'validating', 'blocked'];
    case 'failed':
      return [...success.slice(0, success.indexOf('verifying') + 1), 'failed'];
    case 'rolling_back':
      return [...success.slice(0, success.indexOf('cutover') + 1), 'rolling_back'];
    case 'rolled_back':
      return [...success.slice(0, success.indexOf('verifying') + 1), 'rolling_back', 'rolled_back'];
    case 'cancelled':
      return ['pending', 'validating', 'ready', 'cancelled'];
    default: {
      const index = success.indexOf(phase);
      return index === -1 ? [phase] : success.slice(0, index + 1);
    }
  }
}

const REASONS: Partial<Record<Phase, string>> = {
  validating: 'plan validation started',
  ready: 'pre-flight passed',
  blocked: 'blocker findings present',
  precopy: 'wave started',
  syncing: 'pass 1 complete; delta passes until convergence',
  awaiting_cutover: 'converged: last delta below the threshold',
  cutover: 'cutover gate satisfied (approved, window open)',
  verifying: 'destination server created',
  completed: 'verification passed',
  finalized: 'finalized by approver',
  failed: 'step failed',
  rolling_back: 'rollback requested',
  rolled_back: 'source VM running again',
  cancelled: 'cancelled by operator',
};

/**
 * Phase entries along `path`: known milestones (`anchors`: pass boundaries, downtime start/end, the
 * current phase) are used as-is; the others are interpolated between their neighbours.
 */
function synthesizeHistory(path: Phase[], anchors: Map<Phase, number>, createdAt: number, actorFor: (p: Phase) => string): PhaseChange[] {
  const times: Array<number | null> = path.map((phase, i) => (i === 0 ? createdAt : (anchors.get(phase) ?? null)));
  for (let i = 1; i < times.length; i += 1) {
    if (times[i] !== null) continue;
    let j = i + 1;
    while (j < times.length && times[j] === null) j += 1;
    const prev = times[i - 1] as number;
    const next = j < times.length ? (times[j] as number) : prev + 60_000 * (j - i + 1);
    const steps = j - i + 1;
    for (let k = i; k < j; k += 1) times[k] = prev + ((next - prev) * (k - i + 1)) / steps;
    i = j - 1;
  }
  for (let i = 1; i < times.length; i += 1) {
    if ((times[i] as number) <= (times[i - 1] as number)) times[i] = (times[i - 1] as number) + 1000;
  }
  return path.map((to, i) => ({
    from_phase: i === 0 ? null : (path[i - 1] ?? null),
    to_phase: to,
    at: iso(times[i] as number),
    reason: i === 0 ? 'migration created' : (REASONS[to] ?? 'transition'),
    actor: actorFor(to),
  }));
}

interface ScheduledPass {
  spec: NonNullable<MigrationSpec['passes']>[number];
  start: number;
  end: number | null;
}

/**
 * Lays warm passes out backwards from the point where the migration converged (or the open pass
 * started), and the final pass right after the source VM stopped.
 */
function schedulePasses(m: MigrationSpec, enteredAt: number, downtimeStartedAt: number | null): ScheduledPass[] {
  const specs = m.passes ?? [];
  const open = specs.find((p) => p.open && p.kind !== 'final');
  const final = specs.find((p) => p.kind === 'final');
  const done = specs.filter((p) => !p.open && p.kind !== 'final');
  let cursor: number;
  if (open) cursor = enteredAt - 60_000;
  else if (m.phase === 'awaiting_cutover') cursor = enteredAt - 30_000;
  else if (downtimeStartedAt !== null) cursor = downtimeStartedAt - 20 * 60_000;
  else cursor = enteredAt;
  const scheduled: ScheduledPass[] = [];
  for (const spec of [...done].reverse()) {
    const end = cursor;
    const start = end - spec.durationS * 1000;
    scheduled.unshift({ spec, start, end });
    cursor = start - 60_000;
  }
  if (open) scheduled.push({ spec: open, start: enteredAt, end: null });
  if (final && downtimeStartedAt !== null) {
    const start = downtimeStartedAt + 20_000;
    scheduled.push({ spec: final, start, end: final.open ? null : start + final.durationS * 1000 });
  }
  return scheduled;
}

export interface Fixtures {
  providers: Provider[];
  inventories: Record<string, VMRef[] | DestinationInventory>;
  plans: Plan[];
  migrations: Migration[];
  events: Event[];
}

export function buildFixtures(now: number): Fixtures {
  const providers = buildProviders(now);
  const inventories = buildInventories();
  const plans: Plan[] = [];
  const migrations: Migration[] = [];
  const pending: Array<Omit<Event, 'seq'>> = [];
  const emit = (e: Omit<Event, 'seq'>) => pending.push(e);

  for (const spec of PLAN_SPECS) {
    const createdAt = now - spec.createdAgo * 1000;
    const source = providers.find((p) => p.id === spec.source);
    const destination = providers.find((p) => p.id === spec.destination);
    const vms = (inventories[spec.source] ?? []) as VMRef[];
    const dstInventory = inventories[spec.destination] as DestinationInventory;
    if (!source || !destination) continue;

    const plan: Plan = {
      id: spec.id,
      name: spec.name,
      source_provider_id: spec.source,
      destination_provider_id: spec.destination,
      vm_ids: spec.migrations.map((m) => m.vm),
      status: spec.status,
      ...defaultPlanFields(createdAt),
      ...spec.overrides,
      description: spec.description,
      waves: spec.waves.map((w, i) => ({ id: w.id, name: w.name, order: i + 1, vm_ids: w.vms, depends_on: w.depends_on ?? [], max_parallel: w.max_parallel ?? 5 })),
      updated_at: iso(now - 5 * 60_000),
    };
    if (spec.id === 'plan-4f2a9c1e') plan.cutover_window = { start: iso(now - 1 * H * 1000), end: iso(now + 5 * H * 1000) };
    if (spec.id === 'plan-7b3e0d52') plan.cutover_window = { start: iso(now + 2 * D * 1000), end: iso(now + 2 * D * 1000 + 6 * H * 1000) };
    plans.push(plan);
    emit({ ts: iso(createdAt), kind: 'plan.created', plan_id: plan.id, migration_id: null, actor: 'rina', message: `Plan "${plan.name}" created with ${plan.vm_ids.length} VMs`, data: { status: 'draft' } });

    const selectedNames = plan.vm_ids.map((id) => vms.find((v) => v.source_id === id)?.name ?? id);
    for (const m of spec.migrations) {
      const vm = vms.find((v) => v.source_id === m.vm);
      if (!vm) continue;
      const findings = preflight(vm, source.kind, plan, dstInventory.networks, selectedNames);
      const elig = eligibility(vm, source.kind, plan, source, destination, findings);
      const estimates = (Object.keys(elig) as Strategy[]).map((s) => estimate(vm, s, plan, elig[s] ?? []));
      const chosen = estimates.find((e) => e.strategy === m.strategy) ?? null;
      const enteredAt = now - m.enteredAgo * 1000;
      const migCreatedAt = Math.min(createdAt + 60_000, enteredAt - 60_000);
      const path = walkTo(m.strategy, m.phase);
      const downtimeStartedAt = m.downtimeStartedAgo != null ? now - m.downtimeStartedAgo * 1000 : null;
      const downtimeEndedAt = downtimeStartedAt !== null && m.downtimeS != null ? downtimeStartedAt + m.downtimeS * 1000 : null;
      const scheduled = schedulePasses(m, enteredAt, downtimeStartedAt);

      // Milestones that phase entries must line up with.
      const anchors = new Map<Phase, number>();
      const firstPass = scheduled[0];
      if (firstPass && path.includes('precopy')) anchors.set('precopy', firstPass.start);
      const fullPass = scheduled.find((x) => x.spec.kind === 'full');
      if (fullPass?.end != null && path.includes('syncing')) anchors.set('syncing', fullPass.end + 5_000);
      const lastDelta = [...scheduled].reverse().find((x) => x.spec.kind !== 'final' && x.end !== null);
      if (lastDelta?.end != null && path.includes('awaiting_cutover') && m.phase !== 'precopy' && m.phase !== 'syncing') {
        anchors.set('awaiting_cutover', lastDelta.end + 5_000);
      }
      if (downtimeStartedAt !== null && path.includes('cutover')) anchors.set('cutover', downtimeStartedAt);
      if (downtimeEndedAt !== null && path.includes('completed')) anchors.set('completed', downtimeEndedAt);
      if (downtimeEndedAt !== null && path.includes('rolled_back')) anchors.set('rolled_back', downtimeEndedAt);
      if (!anchors.has(m.phase)) anchors.set(m.phase, enteredAt);
      const history = synthesizeHistory(path, anchors, migCreatedAt, (p) =>
        p === 'finalized' ? 'sari' : p === 'cancelled' || p === 'rolling_back' ? 'bayu' : 'orchestrator',
      );
      const bytesTotal = m.strategy === 'storage_handover' ? 0 : vm.used_bytes;
      const progress = m.progress ?? 0;
      const syncPasses: SyncPass[] = scheduled.map(({ spec: p, start, end }, i) => {
        const changed = Math.round(p.changedGiB * GiB);
        return {
          number: i + 1,
          kind: p.kind,
          started_at: iso(start),
          ended_at: end === null ? null : iso(end),
          bytes_scanned: end === null ? Math.round((vm.disk_bytes * progress) / 100) : vm.disk_bytes,
          bytes_changed: end === null ? 0 : changed,
          bytes_transferred: end === null ? Math.round(((bytesTotal * progress) / 100) * (p.kind === 'full' ? 1 : 0.05)) : Math.round(changed * 0.98),
          duration_s: end === null ? null : p.durationS,
        };
      });
      const migration: Migration = {
        id: m.id,
        plan_id: plan.id,
        wave_id: m.wave ?? null,
        vm,
        strategy: m.strategy,
        phase: m.phase,
        phase_history: history,
        progress_pct: progress,
        bytes_total: bytesTotal,
        bytes_transferred: Math.round((bytesTotal * progress) / 100),
        sync_passes: syncPasses,
        estimate: chosen,
        estimates,
        // a delta pass calibrates the per-stream scan rate (SDD §9.1); pass 1 alone does not
        observed_scan_bps: syncPasses.filter((p) => p.kind !== 'full' && p.ended_at !== null).length > 0 ? 545 * MiB : null,
        resolved_mappings: { networks: {}, flavors: {}, volume_types: {}, projects: {} },
        findings,
        checkpoint: m.checkpoint ?? null,
        downtime_started_at: downtimeStartedAt === null ? null : iso(downtimeStartedAt),
        downtime_ended_at: downtimeEndedAt === null ? null : iso(downtimeEndedAt),
        actual_downtime_s: m.downtimeS ?? null,
        approvals: (m.approvals ?? []).map((a) => ({ actor: a.actor, at: iso(now - a.ago * 1000), comment: a.comment ?? null })),
        cutover_requested: m.cutoverRequested ?? false,
        force_window: false,
        advisor_notes: (m.notes ?? []).map(({ ago, ...note }) => ({ ...note, created_at: iso(now - ago * 1000) })),
        review_required: Boolean(m.reviewReason),
        review_reason: m.reviewReason ?? null,
        destination_server_id: m.destination ?? null,
        error: m.error ?? null,
        attempts: m.attempts ?? 0,
        created_at: iso(migCreatedAt),
        updated_at: iso(enteredAt),
      };
      migrations.push(migration);

      emit({ ts: iso(migCreatedAt), kind: 'migration.created', plan_id: plan.id, migration_id: m.id, actor: 'orchestrator', message: `${vm.name}: migration created (${m.strategy})`, data: { strategy: m.strategy } });
      for (const change of history.slice(1)) {
        emit({ ts: change.at, kind: 'migration.phase', plan_id: plan.id, migration_id: m.id, actor: change.actor, message: `${vm.name}: ${change.from_phase} → ${change.to_phase}`, data: { from_phase: change.from_phase, to_phase: change.to_phase, reason: change.reason } });
      }
      for (const pass of syncPasses.filter((p) => p.ended_at)) {
        emit({ ts: pass.ended_at ?? pass.started_at, kind: 'migration.sync_pass', plan_id: plan.id, migration_id: m.id, actor: 'orchestrator', message: `${vm.name}: pass ${pass.number} (${pass.kind}) changed ${(pass.bytes_changed / GiB).toFixed(2)} GiB`, data: { pass } });
      }
      for (const approval of migration.approvals) {
        emit({ ts: approval.at, kind: 'migration.approved', plan_id: plan.id, migration_id: m.id, actor: approval.actor, message: `${vm.name}: approved by ${approval.actor}`, data: { comment: approval.comment } });
      }
      if (migration.downtime_started_at) {
        emit({ ts: migration.downtime_started_at, kind: 'migration.downtime_started', plan_id: plan.id, migration_id: m.id, actor: 'orchestrator', message: `${vm.name}: source VM stopped — downtime clock started`, data: {} });
      }
      if (migration.downtime_ended_at) {
        emit({ ts: migration.downtime_ended_at, kind: 'migration.downtime_ended', plan_id: plan.id, migration_id: m.id, actor: 'orchestrator', message: `${vm.name}: downtime ended after ${m.downtimeS} s`, data: { actual_downtime_s: m.downtimeS } });
      }
      if (migration.error) {
        emit({ ts: iso(enteredAt - 1000), kind: 'migration.error', plan_id: plan.id, migration_id: m.id, actor: 'orchestrator', message: `${vm.name}: ${migration.error}`, data: {} });
      }
      for (const note of migration.advisor_notes) {
        const kind = note.kind === 'screen' ? 'advisor.verification' : `advisor.${note.kind}`;
        emit({ ts: note.created_at, kind, plan_id: plan.id, migration_id: m.id, actor: note.source === 'memory' ? 'agentmemory' : 'advisor', message: `${vm.name}: ${note.summary}`, data: { source: note.source, confidence: note.confidence } });
      }
    }

    if (plan.status !== 'draft') emit({ ts: iso(createdAt + 30 * 60_000), kind: 'plan.validated', plan_id: plan.id, migration_id: null, actor: 'rina', message: `Plan "${plan.name}" validated`, data: {} });
    if (['running', 'paused', 'completed', 'failed'].includes(plan.status))
      emit({ ts: iso(createdAt + 60 * 60_000), kind: 'plan.started', plan_id: plan.id, migration_id: null, actor: 'bayu', message: `Plan "${plan.name}" started`, data: {} });
    if (plan.status === 'paused') emit({ ts: iso(now - 20 * 60_000), kind: 'plan.paused', plan_id: plan.id, migration_id: null, actor: 'bayu', message: `Plan "${plan.name}" paused: vCenter degraded`, data: {} });
    if (plan.status === 'completed') emit({ ts: iso(now - 7 * D * 1000), kind: 'plan.completed', plan_id: plan.id, migration_id: null, actor: 'orchestrator', message: `Plan "${plan.name}" completed`, data: {} });
  }

  for (const provider of providers.filter((p) => p.last_checked_at)) {
    emit({ ts: provider.last_checked_at ?? iso(now), kind: 'provider.checked', plan_id: null, migration_id: null, actor: 'bayu', message: `${provider.name}: ${provider.status}`, data: { status: provider.status } });
  }
  emit({ ts: iso(now - 52 * 60_000), kind: 'auth.denied', plan_id: null, migration_id: null, actor: 'anonymous', message: 'Rejected request with an invalid bearer token', data: { path: '/api/v1/plans' } });
  emit({ ts: iso(now - 7 * D * 1000 + 600_000), kind: 'memory.lesson_saved', plan_id: 'plan-2d5c8b93', migration_id: 'mig-d4e6f8a091', actor: 'agentmemory', message: 'Lesson saved: warm, <50G, 3 passes, estimate 3m 40s vs actual 3m 08s', data: { type: 'fact' } });

  pending.sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts));
  const events: Event[] = pending.map((e, i) => ({ ...e, seq: i + 1 }));
  return { providers, inventories, plans, migrations, events };
}

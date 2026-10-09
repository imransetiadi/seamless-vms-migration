import type { Distribution, ProviderKind, ProviderRole } from '../api/types';

/** Credential methods the dialog offers (SDD §13.3). */
export type CredentialMode = 'password' | 'application_credential' | 'clouds_yaml' | 'vcenter';

export interface DistributionPreset {
  id: Distribution;
  /** Two letters for the platform mark. */
  mark: string;
  label: string;
  /** One line that tells operators which platform this is. */
  summary: string;
  kind: ProviderKind;
  roles: ProviderRole[];
  endpointExample: string;
  endpointHint: string;
  caHint: string;
  credentialModes: CredentialMode[];
  /** Conversion hosts are deployed by Seamless (OpenStack family) or pre-existing (VMware). */
  managedConversionHost: boolean;
}

/** Presets per platform; they only fill the form, the API decides by `kind` (SDD §4.2). */
export const DISTRIBUTION_PRESETS: DistributionPreset[] = [
  {
    id: 'rhosp',
    mark: 'RH',
    label: 'Red Hat OpenStack 17.1',
    summary: 'Director-deployed overcloud; public Keystone on port 13000 with TLS.',
    kind: 'openstack',
    roles: ['source'],
    endpointExample: 'https://overcloud.example.com:13000/v3',
    endpointHint: 'The public Keystone endpoint of the overcloud (port 13000).',
    caHint: 'The overcloud CA, e.g. the file referenced by OS_CACERT in overcloudrc.',
    credentialModes: ['password', 'application_credential', 'clouds_yaml'],
    managedConversionHost: true,
  },
  {
    id: 'openstack_community',
    mark: 'OS',
    label: 'OpenStack Community',
    summary: 'Upstream OpenStack, any deployment tool; Keystone on port 5000.',
    kind: 'openstack',
    roles: ['source', 'destination'],
    endpointExample: 'https://openstack.example.com:5000/v3',
    endpointHint: 'The Keystone v3 endpoint (port 5000 unless a proxy publishes it elsewhere).',
    caHint: 'The CA that signed the Keystone certificate, when it is not publicly trusted.',
    credentialModes: ['password', 'application_credential', 'clouds_yaml'],
    managedConversionHost: true,
  },
  {
    id: 'kolla',
    mark: 'KO',
    label: 'Kolla-Ansible',
    summary: 'Containerised OpenStack deployed by Kolla-Ansible; Keystone behind HAProxy on 5000.',
    kind: 'openstack',
    roles: ['source', 'destination'],
    endpointExample: 'https://kolla-vip.example.com:5000/v3',
    endpointHint: 'The kolla_external_vip_address endpoint (kolla_internal_fqdn for internal access).',
    caHint: 'Kolla’s root CA, usually /etc/kolla/certificates/ca/root.crt on the deploy host.',
    credentialModes: ['password', 'application_credential', 'clouds_yaml'],
    managedConversionHost: true,
  },
  {
    id: 'vmware',
    mark: 'VM',
    label: 'VMware vCenter',
    summary: 'vSphere VMs migrated with the VMware migration kit; CBT for warm migration.',
    kind: 'vmware',
    roles: ['source'],
    endpointExample: 'https://vcenter.example.com/sdk',
    endpointHint: 'The vCenter SDK URL. Use a service account with read access and snapshot rights.',
    caHint: 'The vCenter machine SSL CA, when vCenter uses its own VMCA.',
    credentialModes: ['vcenter'],
    managedConversionHost: false,
  },
  {
    id: 'rhoso',
    mark: 'RO',
    label: 'RHOSO 18.0',
    summary: 'Red Hat OpenStack Services on OpenShift, the destination; Keystone published as a route.',
    kind: 'rhoso',
    roles: ['destination'],
    endpointExample: 'https://keystone-public-openstack.apps.cluster.example.com/v3',
    endpointHint: 'The keystone-public route (oc get route keystone-public -n openstack).',
    caHint: 'The OpenShift ingress CA when the route certificate is not publicly trusted.',
    credentialModes: ['password', 'application_credential', 'clouds_yaml'],
    managedConversionHost: true,
  },
];

export function presetFor(distribution: Distribution | null | undefined, kind?: ProviderKind): DistributionPreset {
  const exact = DISTRIBUTION_PRESETS.find((p) => p.id === distribution);
  if (exact) return exact;
  if (kind === 'vmware') return DISTRIBUTION_PRESETS.find((p) => p.id === 'vmware')!;
  if (kind === 'rhoso') return DISTRIBUTION_PRESETS.find((p) => p.id === 'rhoso')!;
  return DISTRIBUTION_PRESETS.find((p) => p.id === 'openstack_community')!;
}

/** A provider id from a display name (`^[a-z0-9][a-z0-9-]{1,62}$`). */
export function slugify(name: string): string {
  return name
    .toLowerCase()
    .normalize('NFKD')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 63)
    .replace(/-+$/, '');
}

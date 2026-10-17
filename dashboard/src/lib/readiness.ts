import type { Nic, ProviderKind, Severity, VMRef } from '../api/types';
import { guestOsOf } from './guestOs';
import { kitVmName } from './kitName';

/**
 * Inventory readiness hints derived client-side from VMRef fields. They mirror SDD §9.3 finding
 * codes that need no destination data; authoritative findings come from plan validation.
 */
export interface ReadinessFlag {
  code: string;
  label: string;
  severity: Severity;
  detail: string;
}

export type ReadinessLevel = 'ready' | 'attention' | 'blocker';

const SRIOV = new Set(['direct', 'direct-physical', 'macvtap']);

/** NICs os-migrate cannot recreate through Nova, which takes exactly one fixed IP per port (SDD §9.2). */
export function portsWithoutOneFixedIp(vm: VMRef): Nic[] {
  return vm.nics.filter((n) => n.fixed_ips.length !== 1);
}

/** Each NIC by its network and addresses, as the API words it: "app-net (10.0.0.5, fd00::5)". */
export function describePorts(nics: Nic[]): string {
  return nics.map((n) => `${n.network} (${n.fixed_ips.join(', ') || 'no fixed IP'})`).join(', ');
}

export function readinessFlags(vm: VMRef, kind: ProviderKind): ReadinessFlag[] {
  const flags: ReadinessFlag[] = [];
  const add = (code: string, label: string, severity: Severity, detail: string) => flags.push({ code, label, severity, detail });

  if (vm.power_state === 'error') add('SRC_VM_ERROR_STATE', 'Error state', 'blocker', 'The source VM is in ERROR state.');
  if (vm.power_state === 'transitioning') add('SRC_VM_TRANSITIONAL_STATE', 'Task in flight', 'blocker', 'A Nova task (resize, migration, rescue, rebuild, reboot) is in flight.');
  if ('resources:VGPU' in vm.flavor_extra_specs) add('VM_VGPU', 'vGPU', 'blocker', 'The flavor requests a vGPU.');
  if ('pci_passthrough:alias' in vm.flavor_extra_specs) add('VM_PCI_PASSTHROUGH', 'PCI passthrough', 'blocker', 'The flavor requests PCI passthrough.');
  if (vm.disks.some((d) => d.multiattach)) add('SRC_VM_MULTIATTACH', 'Multi-attach', 'warning', 'A multi-attach volume rules out warm and storage handover.');
  if (vm.disks.some((d) => d.encrypted)) add('VOL_ENCRYPTED', 'Encrypted volume', 'warning', 'The Barbican key must be re-created on the destination.');
  const guest = guestOsOf(vm);
  if (guest.lifecycle === 'legacy') add('GUEST_OS_LEGACY', 'Legacy OS', 'warning', `${guest.label} is out of vendor support; it still migrates — test the application on the destination.`);
  if (guest.family === 'unknown') add('GUEST_OS_UNKNOWN', 'Unknown OS', 'info', 'The guest OS is not identified; set os_distro/os_version or run VMware Tools.');
  if (vm.nics.some((n) => SRIOV.has(n.vnic_type))) add('NET_SRIOV_PORT', 'SR-IOV port', 'warning', 'SR-IOV ports must be re-created manually.');
  const oddPorts = portsWithoutOneFixedIp(vm);
  if (kind !== 'vmware' && oddPorts.length > 0) {
    add(
      'NET_PORT_IPS',
      'Port IPs',
      'warning',
      oddPorts.length === 1
        ? `Port ${describePorts(oddPorts)} keeps every address only with the plan option "Keep MAC addresses and every fixed IP": without it, cold and warm cannot recreate it and storage handover keeps one of its addresses.`
        : `Ports ${describePorts(oddPorts)} keep every address only with the plan option "Keep MAC addresses and every fixed IP": without it, cold and warm cannot recreate them and storage handover keeps one address per port.`,
    );
  }
  if (kind === 'vmware') {
    // the kit names the VM's server, ports and volumes after the VM (SDD §9.3, review RV28-03)
    if (kitVmName(vm.name) === '') add('SRC_VM_NAME_UNUSABLE', 'Unusable name', 'blocker', "The migration kit keeps no character of the name: rename the VM in vCenter so that it holds a Latin letter or a digit.");
    if (vm.cbt_enabled === false) add('VMW_CBT_DISABLED', 'CBT off', 'warning', 'Changed Block Tracking is off: only cold migration.');
    if (vm.disks.some((d) => d.independent)) add('VMW_INDEPENDENT_DISK', 'Independent disk', 'warning', 'Independent disks cannot be tracked by CBT.');
    if (vm.snapshot_count > 0) {
      add('VMW_SNAPSHOTS_PRESENT', `${vm.snapshot_count} snapshot${vm.snapshot_count === 1 ? '' : 's'}`, 'warning', 'Consolidate VMware snapshots before migrating.');
    }
    if (vm.tools_ok === false) add('VMW_TOOLS_MISSING', 'Tools missing', 'info', 'VMware Tools are not running.');
    if (guest.v2v === 'unsupported') add('GUEST_CONVERSION_UNSUPPORTED', 'Driver prep needed', 'warning', `virt-v2v cannot prepare ${guest.label}: install older virtio-win drivers in the guest first.`);
    if (guest.v2v === 'tech_preview' || guest.v2v === 'unverified') add('GUEST_CONVERSION_UNVERIFIED', 'Test conversion', 'warning', `Converting ${guest.label} is ${guest.v2v === 'tech_preview' ? 'a Technology Preview' : 'not supported by Red Hat'}: test a copy first.`);
  }
  const root = vm.disks[0];
  if (root && (root.kind === 'image_root' || root.kind === 'ephemeral')) {
    add('SRC_VM_EPHEMERAL_ROOT', 'Image-backed root', 'info', 'Warm passes snapshot the server to Glance.');
  }
  return flags;
}

export function readinessLevel(flags: ReadinessFlag[]): ReadinessLevel {
  if (flags.some((f) => f.severity === 'blocker')) return 'blocker';
  if (flags.some((f) => f.severity === 'warning')) return 'attention';
  return 'ready';
}

/**
 * Guest OS catalog — the same rules as `seamless_migrate.guest_os` (SDD §9.5), used by the mock
 * adapter and as a fallback when an API response predates `VMRef.guest_os`. Both test suites run
 * the case table in `seamless/tests/fixtures/guest_os_cases.json`.
 */
import type { GuestOS, VMRef } from '../api/types';

const UBUNTU_CODENAMES: Record<string, string> = {
  trusty: '14.04', xenial: '16.04', bionic: '18.04', focal: '20.04', jammy: '22.04', noble: '24.04', plucky: '25.04', questing: '25.10',
};
const DEBIAN_CODENAMES: Record<string, string> = {
  wheezy: '7', jessie: '8', stretch: '9', buster: '10', bullseye: '11', bookworm: '12', trixie: '13', forky: '14',
};
const LINUX: Array<[string, RegExp]> = [
  ['centos-stream', /centos[\s_-]*stream/],
  ['rocky', /rocky/],
  ['almalinux', /alma/],
  ['oracle', /oracle|\boel\b/],
  ['centos', /centos/],
  ['rhel', /rhel|red\s*hat/],
  ['ubuntu', /ubuntu/],
  ['debian', /debian/],
  ['sles', /sles|suse\s+linux\s+enterprise/],
  ['opensuse', /opensuse/],
  ['fedora', /fedora/],
];
const LABELS: Record<string, string> = {
  rhel: 'RHEL', centos: 'CentOS', 'centos-stream': 'CentOS Stream', rocky: 'Rocky Linux', almalinux: 'AlmaLinux', oracle: 'Oracle Linux',
  ubuntu: 'Ubuntu', debian: 'Debian', sles: 'SLES', opensuse: 'openSUSE', fedora: 'Fedora', 'windows-server': 'Windows Server', windows: 'Windows',
};
const VMWARE_WINDOWS: Record<string, [string, string]> = {
  windows2022srvnext64guest: ['windows-server', '2025'],
  windows2019srvnext64guest: ['windows-server', '2022'],
  windows2019srv64guest: ['windows-server', '2019'],
  windows9server64guest: ['windows-server', '2016'],
  windows8server64guest: ['windows-server', '2012'],
  windows7server64guest: ['windows-server', '2008 R2'],
  windows1164guest: ['windows', '11'],
  windows964guest: ['windows', '10'],
  windows9guest: ['windows', '10'],
  windows864guest: ['windows', '8'],
  windows764guest: ['windows', '7'],
  windows7guest: ['windows', '7'],
};

type Pair = [string | null, string | null];

const major = (version: string | null): number | null => {
  const m = version?.match(/^\d+/);
  return m ? Number(m[0]) : null;
};

function windows(low: string, compact: string): Pair {
  if (compact in VMWARE_WINDOWS) return VMWARE_WINDOWS[compact]!;
  if (compact.startsWith('winlonghorn')) return ['windows-server', '2008'];
  if (compact.startsWith('winnet')) return ['windows-server', '2003'];
  const lib = compact.match(/^win2k(\d{1,2})(r2)?$/);
  if (lib) return ['windows-server', `${2000 + Number(lib[1])}${lib[2] ? ' R2' : ''}`];
  const year = /(2003|2008|2012|2016|2019|2022|2025)/.exec(low);
  if (year) {
    const r2 = low.slice(year.index + year[0].length).includes('r2');
    return ['windows-server', `${year[0]}${r2 ? ' R2' : ''}`];
  }
  const client = low.match(/windows[\s_-]*(11|10|8\.1|8|7)\b/);
  if (client) return ['windows', client[1]!];
  return [null, null];
}

function linux(low: string): Pair {
  const text = low.replace(/_?64guest$|guest$/, '');
  for (const [distro, pattern] of LINUX) {
    const match = pattern.exec(text);
    if (!match) continue;
    const number = text.slice(match.index + match[0].length).match(/(\d+(?:\.\d+)?)/);
    let version: string | null = number ? number[1]! : null;
    if (distro === 'debian' && version) version = version.split('.')[0]!;
    if (version === null) {
      const names = distro === 'ubuntu' ? UBUNTU_CODENAMES : DEBIAN_CODENAMES;
      version = Object.entries(names).find(([name]) => text.includes(name))?.[1] ?? null;
    }
    if (distro === 'ubuntu' && version?.includes('.')) version = version.split('.').slice(0, 2).join('.');
    return [distro, version];
  }
  for (const [names, distro] of [[UBUNTU_CODENAMES, 'ubuntu'], [DEBIAN_CODENAMES, 'debian']] as const) {
    for (const [name, version] of Object.entries(names)) {
      if (new RegExp(`\\b${name}\\b`).test(text)) return [distro, version];
    }
  }
  return [null, null];
}

function lifecycle(distro: string | null, version: string | null): GuestOS['lifecycle'] {
  const m = major(version);
  switch (distro) {
    case 'rhel':
    case 'oracle':
      return m === null ? 'unknown' : m <= 7 ? 'legacy' : 'current';
    case 'centos':
      return 'legacy';
    case 'centos-stream':
      return m === null ? 'unknown' : m <= 8 ? 'legacy' : 'current';
    case 'rocky':
    case 'almalinux':
      return m === null ? 'unknown' : 'current';
    case 'ubuntu': {
      if (!version || !version.includes('.') || m === null) return 'unknown';
      const minor = Number(version.split('.')[1]);
      const lts = minor === 4 && m % 2 === 0;
      return m > 26 || (m === 26 && minor >= 4) || (lts && m >= 22) ? 'current' : 'legacy';
    }
    case 'debian':
      return m === null ? 'unknown' : m <= 11 ? 'legacy' : 'current';
    case 'sles':
      return m === null ? 'unknown' : m <= 12 ? 'legacy' : 'current';
    case 'windows-server':
      return m === null ? 'unknown' : m <= 2012 ? 'legacy' : 'current';
    case 'windows':
      return m === null ? 'unknown' : m >= 11 ? 'current' : 'legacy';
    default:
      return 'unknown';
  }
}

function v2v(distro: string | null, version: string | null): GuestOS['v2v'] {
  const m = major(version);
  switch (distro) {
    case 'rhel':
      return m === null ? 'unverified' : m >= 7 ? 'supported' : m === 6 ? 'unverified' : 'unsupported';
    case 'centos':
      return m !== null && m <= 5 ? 'unsupported' : 'unverified';
    case 'centos-stream':
    case 'rocky':
    case 'almalinux':
    case 'oracle':
    case 'sles':
    case 'opensuse':
    case 'fedora':
      return 'unverified';
    case 'ubuntu':
    case 'debian':
      return 'tech_preview';
    case 'windows-server':
      return m === null ? 'unknown' : m >= 2016 ? 'supported' : 'unsupported';
    case 'windows':
      return m === null ? 'unknown' : m >= 10 ? 'supported' : 'unsupported';
    default:
      return 'unknown';
  }
}

export function identifyGuestOs(osType: string | null | undefined): GuestOS {
  const raw = (osType ?? '').trim();
  const base = { distro: null, version: null, lifecycle: 'unknown', v2v: 'unknown' } as const;
  if (!raw) return { family: 'unknown', ...base, label: 'Unknown OS' };
  const low = raw.toLowerCase();
  const compact = low.replace(/[^a-z0-9]/g, '');
  let family: GuestOS['family'];
  let pair: Pair;
  if (low.startsWith('win') || low.includes('windows') || low.includes('microsoft')) {
    pair = windows(low, compact);
    family = 'windows';
  } else {
    pair = linux(low);
    family = pair[0] || low.includes('linux') ? 'linux' : 'unknown';
  }
  if (family === 'unknown') return { family, ...base, label: raw };
  const [distro, version] = pair;
  if (!distro) return { family, ...base, label: family === 'windows' ? 'Windows' : 'Linux' };
  return {
    family,
    distro,
    version,
    label: `${LABELS[distro]}${version ? ` ${version}` : ''}`,
    lifecycle: lifecycle(distro, version),
    v2v: v2v(distro, version),
  };
}

/** The VM's guest OS as the API reports it, or identified locally from `os_type`. */
export function guestOsOf(vm: Pick<VMRef, 'os_type'> & { guest_os?: GuestOS }): GuestOS {
  return vm.guest_os ?? identifyGuestOs(vm.os_type);
}

export const V2V_LABELS: Record<GuestOS['v2v'], string> = {
  supported: 'Conversion supported',
  tech_preview: 'Conversion is a Technology Preview',
  unverified: 'Conversion not supported by Red Hat',
  unsupported: 'Needs driver preparation before conversion',
  unknown: 'Conversion support unknown',
};

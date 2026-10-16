/**
 * The mock's port of the control plane's tier rules (planning/waves.py `heuristic_tier`, SDD §9.4) and
 * probe suggestions (planning/probes.py, §9.6, ADV-18). Both suites test it against one case table,
 * `seamless/tests/fixtures/probe_suggestion_cases.json`.
 */
import { identifyGuestOs } from '../lib/guestOs';
import type { AppProbe, VMRef } from './types';

const TIERS = ['stateless_web', 'middleware_queue', 'infrastructure_service', 'stateful_database', 'legacy_os', 'manual_review'] as const;
type Tier = (typeof TIERS)[number];

/** (tier, substrings, whole tokens) — in this order after the legacy-OS check, like the API. */
const RULES: ReadonlyArray<readonly [Tier, readonly string[], ReadonlySet<string>]> = [
  [
    'stateful_database',
    ['database', 'postgres', 'mysql', 'mariadb', 'oracle', 'mongo', 'elastic', 'cassandra', 'mssql', 'couchdb', 'galera', 'sqlserver', 'db2'],
    new Set(['db', 'sql', 'pg', 'ora', 'pgsql']),
  ],
  [
    'middleware_queue',
    ['rabbit', 'kafka', 'activemq', 'artemis', 'queue', 'broker', 'redis', 'memcache', 'cache', 'tomcat', 'jboss', 'wildfly', 'weblogic', 'websphere', 'middleware', 'nats', 'zookeeper'],
    new Set(['mq', 'amq', 'esb', 'app']),
  ],
  [
    'infrastructure_service',
    [
      'ldap', 'freeipa', 'kerberos', 'dns', 'dhcp', 'ntp', 'domain-controller', 'domaincontroller', 'active-directory', 'activedirectory', 'monitor', 'prometheus',
      'grafana', 'zabbix', 'nagios', 'bastion', 'jumphost', 'vault', 'keycloak', 'puppet', 'satellite', 'backup', 'syslog',
    ],
    new Set(['ad', 'dc', 'ipa', 'idm', 'ns', 'ns1', 'ns2', 'jump', 'bind']),
  ],
  ['stateless_web', ['web', 'www', 'nginx', 'httpd', 'apache', 'frontend', 'front-end', 'haproxy', 'portal', 'proxy'], new Set(['api', 'ui', 'lb', 'fe'])],
];

const tcp = (port: number): AppProbe => ({ kind: 'tcp', port, path: '/', expect_status: null });
const http = (port: number): AppProbe => ({ kind: 'http', port, path: '/', expect_status: null });

/** (tier, substrings, whole tokens, probe) — the first match of the VM's tier wins (SDD §9.6). */
const SERVICES: ReadonlyArray<readonly [Tier, readonly string[], ReadonlySet<string>, AppProbe]> = [
  ['stateful_database', ['postgres', 'pgsql'], new Set(['pg']), tcp(5432)],
  ['stateful_database', ['mysql', 'mariadb', 'galera'], new Set(), tcp(3306)],
  ['stateful_database', ['mssql', 'sqlserver'], new Set(), tcp(1433)],
  ['stateful_database', ['oracle'], new Set(['ora']), tcp(1521)],
  ['stateful_database', ['mongo'], new Set(), tcp(27017)],
  ['stateful_database', ['cassandra'], new Set(), tcp(9042)],
  ['stateful_database', ['db2'], new Set(), tcp(50000)],
  ['stateful_database', ['elastic'], new Set(), http(9200)],
  ['stateful_database', ['couchdb'], new Set(), http(5984)],
  ['middleware_queue', ['rabbit'], new Set(), tcp(5672)],
  ['middleware_queue', ['kafka'], new Set(), tcp(9092)],
  ['middleware_queue', ['activemq', 'artemis'], new Set(['amq']), tcp(61616)],
  ['middleware_queue', ['redis'], new Set(), tcp(6379)],
  ['middleware_queue', ['memcache'], new Set(), tcp(11211)],
  ['middleware_queue', ['nats'], new Set(), tcp(4222)],
  ['middleware_queue', ['zookeeper'], new Set(), tcp(2181)],
  ['middleware_queue', ['tomcat', 'jboss', 'wildfly'], new Set(), http(8080)],
  ['middleware_queue', ['weblogic'], new Set(), http(7001)],
  ['middleware_queue', ['websphere'], new Set(), http(9080)],
  ['infrastructure_service', ['ldap', 'freeipa', 'active-directory', 'activedirectory', 'domain-controller', 'domaincontroller'], new Set(['ad', 'dc', 'ipa', 'idm']), tcp(389)],
  ['infrastructure_service', ['dns'], new Set(['bind', 'ns', 'ns1', 'ns2']), tcp(53)],
  ['infrastructure_service', ['kerberos'], new Set(), tcp(88)],
];

function describe(vm: VMRef): { text: string; tokens: Set<string> } {
  const text = [vm.name, ...Object.values(vm.tags), vm.os_type ?? ''].join(' ').toLowerCase();
  return { text, tokens: new Set(text.split(/[^a-z0-9]+/)) };
}

function matches(text: string, tokens: Set<string>, substrings: readonly string[], words: ReadonlySet<string>): boolean {
  return substrings.some((s) => text.includes(s)) || [...words].some((w) => tokens.has(w));
}

/** `heuristic_tier`: an explicit tier tag, a legacy OS, then the rules in order. */
export function tierOf(vm: VMRef): Tier {
  // an empty tier tag falls back to seamless/tier, as the API's `or` (review RV25-02)
  const explicit = vm.tags.tier || vm.tags['seamless/tier'];
  if (explicit && (TIERS as readonly string[]).includes(explicit)) return explicit as Tier;
  if (identifyGuestOs(vm.os_type ?? null).lifecycle === 'legacy') return 'legacy_os';
  const { text, tokens } = describe(vm);
  for (const [tier, substrings, words] of RULES) if (matches(text, tokens, substrings, words)) return tier;
  return 'manual_review';
}

export function isDatabase(vm: VMRef): boolean {
  return tierOf(vm) === 'stateful_database';
}

/** The rules' suggestion for a VM (SDD §9.6); empty when the table names no service of its tier. */
export function suggestProbes(vm: VMRef): AppProbe[] {
  const tier = tierOf(vm);
  if (tier === 'stateless_web') return [http(80)];
  const { text, tokens } = describe(vm);
  const match = SERVICES.find(([t, substrings, words]) => t === tier && matches(text, tokens, substrings, words));
  return match ? [{ ...match[3] }] : [];
}

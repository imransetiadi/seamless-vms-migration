import { CircleAlert, CircleCheck, FileKey2, KeyRound, LockKeyhole, PlugZap, Plus, Trash2, TriangleAlert } from 'lucide-react';
import { useId, useRef, useState, type ChangeEvent, type FormEvent, type ReactNode, type Ref } from 'react';
import { ApiError } from '../api/client';
import { useCheckProvider, useCreateProvider, useInventory, useSetCephKey, useSetConversionKey, useSetProviderCredentials, useUpdateProvider } from '../api/hooks';
import type { CephAccess, ConversionHostConfig, Distribution, Provider, ProviderCredentials, ProviderPatch, ProviderRole } from '../api/types';
import { cn } from '../lib/cn';
import { DISTRIBUTION_PRESETS, presetFor, slugify, type CredentialMode, type DistributionPreset } from '../lib/distributions';
import { formatRelative, namesText } from '../lib/format';
import { inventoryGpuFlavors, sameVgpuProfiles, vgpuProfileErrors, vgpuProfileMap, type VgpuProfileRow } from '../lib/gpu';
import { RELEASE_CHOICES, seriesText } from '../lib/platforms';
import { isBlank, providerLabel } from '../lib/providerUse';
import { providerStatusMeta } from '../lib/status';
import { Button } from './Button';
import { ErrorBanner } from './ErrorBanner';
import { SelectField, TextAreaField, TextField } from './Field';
import { Modal } from './Modal';
import { Segmented } from './Segmented';
import { ProviderStatusBadge } from './StatusBadge';
import { TechPreviewBadge } from './TechPreviewBadge';

const ID_PATTERN = /^[a-z0-9][a-z0-9-]{1,62}$/;
const PLATFORM_LOCKED = 'A provider keeps its platform type; register a new provider instead.';

interface FormState {
  distribution: Distribution;
  name: string;
  id: string;
  idTouched: boolean;
  role: ProviderRole;
  endpoint: string;
  region: string;
  verifyTls: boolean;
  caPath: string;
  mode: CredentialMode;
  cloud: string;
  username: string;
  password: string;
  projectName: string;
  userDomain: string;
  projectDomain: string;
  appId: string;
  appSecret: string;
  iface: '' | 'public' | 'internal' | 'admin';
  datacenter: string;
  hostManaged: boolean;
  hostName: string;
  hostAddress: string;
  hostFlavor: string;
  hostImage: string;
  hostNetwork: string;
  hostUser: string;
  hostCidr: string;
  privateKey: string;
  privateKeyLabel: string;
  /** A secret managed outside Seamless (SDD §13.3): its name, for the credentials and for the key. */
  secretName: string;
  keySource: 'upload' | 'external';
  keySecretName: string;
  /** The series an OpenStack destination declares; '' detects it (SDD §4.2, §10.1). */
  release: string;
  /** The policy defaults an OpenStack or RHOSO destination declares; '' = by kind and series (SDD §4.2, §21.5). */
  policyProfile: '' | 'secure_rbac' | 'legacy';
  /** The vGPU profile each GPU flavor of an OpenStack or RHOSO provider gives, as rows (SDD §4.2, §16, §22.3). */
  gpuProfiles: VgpuProfileRow[];
  /** The source cluster's Ceph access (SDD §4.2, §28.4, plan task CE10): declared or not, its fsid, monitors (one per line), client, and a key to write once. */
  cephEnabled: boolean;
  cephFsid: string;
  cephMonitors: string;
  cephClient: string;
  cephKey: string;
}

/** A field of the form; a vGPU profile row's by its row's key (SDD §16). */
type FieldKey =
  | 'name'
  | 'id'
  | 'endpoint'
  | 'cloud'
  | 'username'
  | 'password'
  | 'projectName'
  | 'appId'
  | 'appSecret'
  | 'hostName'
  | 'hostAddress'
  | 'privateKey'
  | 'secretName'
  | 'keySecretName'
  | 'cephFsid'
  | 'cephMonitors'
  | 'cephClient'
  | 'cephKey'
  | `gpuFlavor-${number}`
  | `gpuProfile-${number}`;

/** A secret name the control plane reads (SDD §13.3); never a path. */
const SECRET_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const SECRET_NAME_RULE = '1–128 letters, digits, ., _ or -, starting with a letter or digit.';
/** On OpenShift a Secret read through the API (not mounted) must say it is meant for providers (SDD §13.3). */
const OPENSHIFT_LABEL = 'On OpenShift, a Secret Seamless reads through the API needs the label seamless.io/provider-secret: "true".';

/** A Ceph cluster's fsid (a UUID), a monitor address ("host" or "host:port") and a Ceph user without "client.", as the API reads them (SDD §4.2, §28.4). */
const CEPH_FSID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const CEPH_MONITOR = /^(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9][A-Za-z0-9.-]{0,252})(?::[0-9]{1,5})?$/;
const CEPH_CLIENT = /^[A-Za-z0-9._-]{1,64}$/;

/** The monitors as typed: one per line, or comma-separated. */
function cephMonitorList(text: string): string[] {
  return text
    .split(/[\n,]/)
    .map((m) => m.trim())
    .filter(Boolean);
}

/** The key as `ceph auth get-key` prints it: base64 of at least 16 bytes and no spaces, as the API checks it (SDD §12, §28.4). */
function cephKeyLooksRight(key: string): boolean {
  if (/\s/.test(key) || key.length % 4 !== 0 || !/^[A-Za-z0-9+/]+={0,2}$/.test(key)) return false;
  const padding = key.endsWith('==') ? 2 : key.endsWith('=') ? 1 : 0;
  return (key.length * 3) / 4 - padding >= 16;
}

/** Whether a declared access changed in what a PATCH may set: never its server-owned key time (SDD §4.2). */
function sameCephAccess(a: Omit<CephAccess, 'key_updated_at'> | null, b: CephAccess | null): boolean {
  if (!a || !b) return !a && !b;
  return a.fsid === b.fsid && a.client === b.client && JSON.stringify(a.monitors) === JSON.stringify(b.monitors);
}

/** Why a named secret is refused, or null (SDD §13.3): the name rule, and no other provider's provider-* secret. */
function secretNameError(name: string, own: string, what: string): string | null {
  const trimmed = name.trim();
  if (!SECRET_NAME.test(trimmed)) return `Enter ${what}: ${SECRET_NAME_RULE}`;
  if (trimmed.startsWith('provider-') && trimmed !== own) {
    return 'This names another provider’s secret: provider-* names are the ones Seamless stores for each provider.';
  }
  return null;
}

/** A secret the dashboard wrote (`provider-{id}`, `provider-{id}-ssh`) is Seamless's own; any other name is managed outside it. */
function outside(name: string | null | undefined, managed: string): string | null {
  return name && name !== managed ? name : null;
}
type Errors = Partial<Record<FieldKey, string>>;

function initialState(provider: Provider | undefined): FormState {
  const preset = presetFor(provider?.distribution, provider?.kind ?? 'openstack');
  const host = provider?.conversion_host;
  const secretName = outside(provider?.credentials_secret, `provider-${provider?.id}`);
  const keySecretName = outside(host?.ssh_key_secret, `provider-${provider?.id}-ssh`);
  const mode: CredentialMode = secretName
    ? 'external_secret'
    : preset.kind === 'vmware'
      ? 'vcenter'
      : provider?.cloud && !provider.credentials_secret
        ? 'clouds_yaml'
        : 'password';
  return {
    distribution: provider?.distribution ?? preset.id,
    name: provider?.name ?? '',
    id: provider?.id ?? '',
    idTouched: Boolean(provider),
    role: provider?.role ?? preset.roles[0] ?? 'source',
    endpoint: provider?.endpoint ?? '',
    region: provider?.region ?? '',
    verifyTls: provider?.verify_tls ?? true,
    caPath: provider?.ca_cert_path ?? '',
    mode,
    cloud: provider?.cloud ?? '',
    username: '',
    password: '',
    projectName: '',
    userDomain: '',
    projectDomain: '',
    appId: '',
    appSecret: '',
    iface: '',
    datacenter: '',
    hostManaged: host ? host.manage : preset.managedConversionHost,
    hostName: host?.name ?? '',
    hostAddress: host?.address ?? '',
    hostFlavor: host?.flavor ?? '',
    hostImage: host?.image ?? '',
    hostNetwork: host?.external_network ?? '',
    hostUser: host?.ssh_user ?? 'cloud-user',
    hostCidr: host?.ssh_allowed_cidr ?? '',
    privateKey: '',
    privateKeyLabel: '',
    secretName: secretName ?? '',
    keySource: keySecretName ? 'external' : 'upload',
    cephEnabled: Boolean(provider?.ceph),
    cephFsid: provider?.ceph?.fsid ?? '',
    cephMonitors: provider?.ceph?.monitors.join('\n') ?? '',
    cephClient: provider?.ceph?.client ?? 'seamless',
    cephKey: '',
    keySecretName: keySecretName ?? '',
    release: provider?.release ?? '',
    policyProfile: provider?.policy_profile ?? '',
    gpuProfiles: Object.entries(provider?.gpu_profiles ?? {}).map(([flavor, profile], key) => ({ key, flavor, profile })),
  };
}

const MODE_LABELS: Record<CredentialMode, string> = {
  password: 'Username and password',
  application_credential: 'Application credential',
  clouds_yaml: 'clouds.yaml entry',
  vcenter: 'vCenter account',
  external_secret: 'Secret managed outside Seamless',
};

function Section({
  title,
  description,
  aside,
  headingRef,
  children,
}: {
  title: string;
  description?: ReactNode;
  /** Beside the heading and outside it, so the section keeps its name: a status badge such as Technology Preview (SDD §16). */
  aside?: ReactNode;
  /** A heading the focus can be put on, when a control of the section goes away (SDD §16). */
  headingRef?: Ref<HTMLHeadingElement>;
  children: ReactNode;
}) {
  const id = useId();
  const heading = (
    <h3 id={id} ref={headingRef} tabIndex={headingRef ? -1 : undefined} className="text-sm font-semibold text-foreground outline-hidden">
      {title}
    </h3>
  );
  return (
    <section aria-labelledby={id} className="flex min-w-0 flex-col gap-3 border-t border-border pt-4 first:border-t-0 first:pt-0">
      <div>
        {aside ? (
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            {heading}
            {aside}
          </div>
        ) : (
          heading
        )}
        {description && <p className="mt-0.5 text-xs text-muted-foreground">{description}</p>}
      </div>
      {children}
    </section>
  );
}

/**
 * The vGPU profiles an OpenStack or RHOSO provider declares for its GPU flavors, as rows of a flavor and the profile it
 * gives (SDD §16, §22.3): each Flavor field suggests the provider's GPU flavors not declared yet, once its inventory is
 * read - a suggestion, never a restriction. Each field names its row ("Flavor 2") for assistive technology.
 */
function VgpuProfiles({
  rows,
  errors,
  offered,
  hint,
  fieldId,
  headingRef,
  onChange,
  onRemove,
}: {
  rows: VgpuProfileRow[];
  errors: Errors;
  /** The provider's GPU flavors its inventory lists; [] until it is read. */
  offered: string[];
  /** What the form knows of them: read, being read, unreadable, none, or a provider not added yet. */
  hint: string;
  fieldId: (key: string) => string;
  headingRef: Ref<HTMLHeadingElement>;
  onChange: (rows: VgpuProfileRow[]) => void;
  onRemove: (key: number) => void;
}) {
  const declared = new Set(rows.map((row) => row.flavor.trim()));
  const listId = fieldId('gpuFlavors');
  const hintId = fieldId('gpuFlavorsHint');
  const set = (key: number, patch: Partial<VgpuProfileRow>) => onChange(rows.map((row) => (row.key === key ? { ...row, ...patch } : row)));
  return (
    <Section
      title="vGPU profiles"
      aside={<TechPreviewBadge />}
      headingRef={headingRef}
      description="The NVIDIA vGPU profile each GPU flavor gives, such as A100-4C: no OpenStack API names it. A flavor that asks for a CUSTOM_ resource class counts as a vGPU only once its profile is declared."
    >
      <p id={hintId} className="text-xs text-muted-foreground">
        {hint}
      </p>
      {offered.length > 0 && (
        <datalist id={listId}>
          {offered
            .filter((flavor) => !declared.has(flavor))
            .map((flavor) => (
              <option key={flavor} value={flavor} />
            ))}
        </datalist>
      )}
      {rows.length === 0 ? (
        <p className="rounded-md border border-dashed border-border px-3 py-2 text-sm text-muted-foreground">No vGPU profile declared.</p>
      ) : (
        <ol className="flex flex-col gap-2">
          {rows.map((row, i) => {
            const n = i + 1;
            return (
              <li key={row.key} className="grid items-start gap-2 rounded-md border border-border p-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]">
                <TextField
                  id={fieldId(`gpuFlavor-${row.key}`)}
                  label={
                    <>
                      Flavor <span className="sr-only">{n}</span>
                    </>
                  }
                  value={row.flavor}
                  onChange={(e) => set(row.key, { flavor: e.target.value })}
                  // no autoComplete="off": a browser may then hide the list's suggestions
                  list={offered.length > 0 ? listId : undefined}
                  aria-describedby={hintId}
                  placeholder="e.g. g1.a100-4c"
                  error={errors[`gpuFlavor-${row.key}`]}
                  inputClassName="font-mono"
                  spellCheck={false}
                />
                <TextField
                  id={fieldId(`gpuProfile-${row.key}`)}
                  label={
                    <>
                      Profile <span className="sr-only">{n}</span>
                    </>
                  }
                  value={row.profile}
                  onChange={(e) => set(row.key, { profile: e.target.value })}
                  placeholder="e.g. A100-4C"
                  error={errors[`gpuProfile-${row.key}`]}
                  inputClassName="font-mono"
                  autoComplete="off"
                  spellCheck={false}
                />
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  icon={Trash2}
                  aria-label={`Remove vGPU profile ${n}`}
                  className="justify-self-end sm:mt-6"
                  onClick={() => onRemove(row.key)}
                />
              </li>
            );
          })}
        </ol>
      )}
      <div>
        <Button
          type="button"
          size="sm"
          icon={Plus}
          onClick={() => onChange([...rows, { key: Math.max(-1, ...rows.map((row) => row.key)) + 1, flavor: '', profile: '' }])}
        >
          Add vGPU profile
        </Button>
      </div>
    </Section>
  );
}

/** The problems of the form's vGPU profile rows, each on its field (SDD §16). */
function rowProblems(rows: readonly VgpuProfileRow[]): Errors {
  const problems: Errors = {};
  for (const { key, field, message } of vgpuProfileErrors(rows)) {
    const at: FieldKey = field === 'flavor' ? `gpuFlavor-${key}` : `gpuProfile-${key}`;
    problems[at] = message;
  }
  return problems;
}

/**
 * The form's problems with its vGPU profile rows judged again (SDD §16): unchanged while the summary holds none of
 * theirs; else theirs as `rows` give them now, or none when the rows are gone with a switch to vCenter (null).
 */
function judgedAgain(errors: Errors, rows: readonly VgpuProfileRow[] | null): Errors {
  const isRow = (field: string) => field.startsWith('gpuFlavor-') || field.startsWith('gpuProfile-');
  if (!Object.keys(errors).some(isRow)) return errors;
  const kept = Object.fromEntries(Object.entries(errors).filter(([field]) => !isRow(field))) as Errors;
  return { ...kept, ...rowProblems(rows ?? []) };
}

/** The platform mark: two letters in a square, the one memorable element of the provider UI. */
export function PlatformMark({ preset, size = 'md' }: { preset: DistributionPreset; size?: 'md' | 'lg' }) {
  return (
    <span
      aria-hidden
      className={cn(
        'inline-flex shrink-0 items-center justify-center rounded-md border border-border bg-muted font-mono font-semibold text-foreground',
        size === 'lg' ? 'size-11 text-base' : 'size-8 text-xs',
      )}
    >
      {preset.mark}
    </span>
  );
}

function PlatformPicker({
  value,
  lockedKind,
  onChange,
}: {
  value: Distribution;
  lockedKind: string | null;
  onChange: (preset: DistributionPreset) => void;
}) {
  const name = useId();
  const reasonId = useId();
  // a platform of another kind stays focusable and says why it cannot be chosen: to assistive
  // technology, on hover, and in a note once it is clicked, tapped or arrowed to (SDD §16)
  const [note, setNote] = useState(false);
  return (
    <div>
      <div role="radiogroup" aria-label="Platform" className="grid gap-2 sm:grid-cols-2">
        {DISTRIBUTION_PRESETS.map((preset) => {
          const locked = lockedKind !== null && preset.kind !== lockedKind;
          const checked = preset.id === value;
          return (
            <label
              key={preset.id}
              className={cn(
                'relative flex min-h-11 cursor-pointer items-start gap-3 rounded-md border p-3 transition-colors duration-150',
                checked ? 'border-accent bg-accent/10' : 'border-border hover:bg-muted/60',
                locked && 'cursor-not-allowed opacity-50 hover:bg-transparent',
                'has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-ring',
              )}
              title={locked ? PLATFORM_LOCKED : undefined}
            >
              <input
                type="radio"
                name={name}
                value={preset.id}
                checked={checked}
                aria-disabled={locked || undefined}
                aria-describedby={locked ? reasonId : undefined}
                // the radio group is controlled: an ignored change leaves the current platform checked
                onChange={() => (locked ? setNote(true) : onChange(preset))}
                className="sr-only"
              />
              <PlatformMark preset={preset} />
              <span className="min-w-0">
                <span className="block text-sm font-medium text-foreground">{preset.label}</span>
                <span className="block text-xs text-muted-foreground">{preset.summary}</span>
              </span>
              {checked && <CircleCheck aria-hidden className="absolute right-2 top-2 size-4 text-accent" />}
            </label>
          );
        })}
      </div>
      {lockedKind !== null && (
        <p id={reasonId} role={note ? 'note' : undefined} className={note ? 'mt-2 text-xs text-muted-foreground' : 'sr-only'}>
          {PLATFORM_LOCKED}
        </p>
      )}
    </div>
  );
}

function Toggle({ checked, onChange, label, hint }: { checked: boolean; onChange: (v: boolean) => void; label: string; hint: string }) {
  return (
    <label className="flex min-h-11 cursor-pointer items-start gap-2.5 py-1">
      <input type="checkbox" className="mt-1 size-4 shrink-0 cursor-pointer accent-accent" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span className="text-sm text-foreground">
        {label}
        <span className="block text-xs text-muted-foreground">{hint}</span>
      </span>
    </label>
  );
}

function StoredNote({ at, what }: { at: string | null | undefined; what: string }) {
  if (!at) return null;
  return (
    <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
      <LockKeyhole aria-hidden className="size-3.5 text-status-success" />
      {what} stored {formatRelative(at)}. Leave the fields empty to keep it; values are never shown again.
    </p>
  );
}

interface Outcome {
  provider: Provider;
  checked: boolean;
}

export interface ProviderDialogProps {
  open: boolean;
  onClose: () => void;
  /** Edit this provider; create a new one when absent. */
  provider?: Provider;
  onSaved?: (provider: Provider, message: string) => void;
}

/**
 * Register or edit a provider (SDD §12): platform preset, connection, write-only sign-in and the
 * conversion host. Credentials and the SSH key go to the secret store and are never read back.
 */
export function ProviderDialog({ open, onClose, provider, onSaved }: ProviderDialogProps) {
  const titleId = useId();
  // a provider this dialog added before a later step failed (credentials, key, a connection test
  // that did not pass): the dialog goes on editing it, so Save never adds it twice (SDD §16)
  const [added, setAdded] = useState<Provider | null>(null);
  const current = provider ?? added ?? undefined;
  const editing = Boolean(current);
  const nameRef = useRef<HTMLInputElement>(null);
  const summaryRef = useRef<HTMLDivElement>(null);
  // the dialog swaps its content in place: the focus follows to the new step (SDD §16)
  const outcomeRef = useRef<HTMLHeadingElement>(null);
  const [form, setForm] = useState<FormState>(() => initialState(provider));
  const [errors, setErrors] = useState<Errors>({});
  const [hostOpen, setHostOpen] = useState(false);
  const [pasteKey, setPasteKey] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [failure, setFailure] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const create = useCreateProvider();
  const update = useUpdateProvider();
  const setCredentials = useSetProviderCredentials();
  const setKey = useSetConversionKey();
  const setCephKey = useSetCephKey();
  const check = useCheckProvider();
  // the provider's GPU flavors, which its vGPU profiles name: read with its inventory while the form shows and nothing
  // saves - each step of a save would read it again, for a source every VM, for a destination Placement too (SDD §16)
  const inventory = useInventory(open && !busy && !outcome && current && current.kind !== 'vmware' ? current.id : undefined);
  const gpuHeadingRef = useRef<HTMLHeadingElement>(null);

  // each opening starts the form afresh, one that mounts open too: reset as the dialog renders, not by an effect a render
  // later (react.dev, "Adjusting some state when a prop changes")
  const [shownOpen, setShownOpen] = useState(false);
  if (open !== shownOpen) {
    setShownOpen(open);
    if (open) {
      setForm(initialState(provider));
      setAdded(null);
      setErrors({});
      setOutcome(null);
      setFailure(null);
      setPasteKey(false);
      setHostOpen(Boolean(provider && presetFor(provider.distribution, provider.kind).kind === 'vmware'));
    }
  }

  const preset = presetFor(form.distribution);
  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => setForm((f) => ({ ...f, [key]: value }));
  // a destination declares its series (OpenStack: RHOSO 18.0 is validated by its kind) and its policy profile (SDD §16)
  const declaresRelease = form.role === 'destination' && preset.kind === 'openstack';
  const declaresProfile = form.role === 'destination' && preset.kind !== 'vmware';
  // an OpenStack or RHOSO provider, source or destination, declares the vGPU profiles of its GPU flavors; vCenter reports
  // each VM's own (SDD §16, §22.3)
  const declaresGpuProfiles = preset.kind !== 'vmware';
  // an OpenStack or RHOSO source declares its Ceph cluster's access for changed-extent tracking (SDD §16, §28.4)
  const declaresCeph = form.role === 'source' && preset.kind !== 'vmware';
  const id = (key: string) => `provider-dialog-${key}`;
  // offered once its inventory is read, never for a provider not added yet; what is not known is said, never read as none
  const offered = current && inventory.data ? inventoryGpuFlavors(inventory.data, current.gpu_profiles ?? {}) : [];
  const gpuHint = !current
    ? 'A provider not added yet offers no GPU flavor: its inventory is read once it is added.'
    : inventory.data
      ? offered.length
        ? `Its GPU flavors: ${namesText(offered)}. Each Flavor field offers those not declared yet.`
        : Array.isArray(inventory.data)
          ? "None of its VMs has a flavor that asks for a GPU: type each flavor's name."
          : "Its inventory lists no flavor that asks for a GPU: type each flavor's name."
      : inventory.error
        ? "Its inventory could not be read, so no GPU flavor is offered: type each flavor's name."
        : 'Reading its inventory for its GPU flavors.';
  const removeGpuProfile = (key: number) => {
    const rows = form.gpuProfiles.filter((row) => row.key !== key);
    setForm((f) => ({ ...f, gpuProfiles: rows }));
    // its problems go with it and the rows left are judged again, as they renumber; the focus moves to the section's
    // heading, never to the page (SDD §16)
    setErrors((e) => judgedAgain(e, rows));
    gpuHeadingRef.current?.focus();
  };

  const choosePreset = (next: DistributionPreset) => {
    // a vCenter's form has no vGPU profiles: their problems go with them, as their fields do (SDD §16)
    if (next.kind === 'vmware') setErrors((e) => judgedAgain(e, null));
    setForm((f) => ({
      ...f,
      distribution: next.id,
      role: next.roles.includes(f.role) ? f.role : (next.roles[0] ?? 'source'),
      mode: next.credentialModes.includes(f.mode) ? f.mode : (next.credentialModes[0] ?? 'password'),
      hostManaged: editing ? f.hostManaged : next.managedConversionHost,
    }));
  };

  const setName = (value: string) =>
    setForm((f) => ({ ...f, name: value, id: f.idTouched || editing ? f.id : slugify(value) }));

  const wantsCredentials = () => {
    if (form.mode === 'clouds_yaml' || form.mode === 'external_secret') return false;
    if (form.mode === 'vcenter') return Boolean(form.username || form.password || form.datacenter);
    if (form.mode === 'application_credential') return Boolean(form.appId || form.appSecret);
    return Boolean(form.username || form.password || form.projectName);
  };

  const validate = (): Errors => {
    const e: Errors = {};
    if (isBlank(form.name)) e.name = 'Enter a name operators will recognise.';
    if (!editing && !ID_PATTERN.test(form.id)) e.id = 'Use 2–63 lowercase letters, digits and dashes, starting with a letter or digit.';
    if (!/^https?:\/\/\S+$/.test(form.endpoint.trim())) e.endpoint = `Enter the full URL, e.g. ${preset.endpointExample}.`;
    const stored = editing && Boolean(current?.credentials_updated_at);
    const required = !editing || !stored || wantsCredentials();
    if (form.mode === 'clouds_yaml') {
      if (!form.cloud.trim()) e.cloud = 'Enter the cloud name of the entry in the mounted clouds.yaml.';
    } else if (form.mode === 'external_secret') {
      const why = secretNameError(form.secretName, `provider-${form.id}`, 'the secret name');
      if (why) e.secretName = why;
    } else if (required && form.mode === 'application_credential') {
      if (!form.appId.trim()) e.appId = 'Enter the application credential ID.';
      if (!form.appSecret) e.appSecret = 'Enter the application credential secret.';
    } else if (required) {
      if (!form.username.trim()) e.username = 'Enter the user name.';
      if (!form.password) e.password = 'Enter the password.';
      if (form.mode === 'password' && !form.projectName.trim()) e.projectName = 'Enter the project the user signs in to.';
    }
    if (!form.hostManaged) {
      if (!form.hostName.trim() && !form.hostAddress.trim()) e.hostAddress = 'Enter the address (or name) of the existing conversion host.';
      const keyStored = editing && Boolean(current?.conversion_key_updated_at);
      if (form.keySource === 'external') {
        const why = secretNameError(form.keySecretName, `provider-${form.id}-ssh`, 'the name of the secret holding the key');
        if (why) e.keySecretName = why;
      } else if (!keyStored && !form.privateKey) e.privateKey = 'Add the SSH private key Seamless uses to reach the conversion host.';
    }
    if (form.keySource === 'upload' && form.privateKey && !/^-----BEGIN [A-Z ]*PRIVATE KEY-----/.test(form.privateKey.trim())) {
      e.privateKey = 'This is not an OpenSSH or PEM private key (it must start with -----BEGIN … PRIVATE KEY-----).';
    }
    // what the API refuses of a declared Ceph access, and the key it needs once (SDD §4.2, §12, §28.4)
    if (declaresCeph && form.cephEnabled) {
      if (!CEPH_FSID.test(form.cephFsid.trim().toLowerCase())) e.cephFsid = "Enter the cluster's fsid: a UUID, as ceph fsid prints it.";
      const monitors = cephMonitorList(form.cephMonitors);
      if (monitors.length === 0 || monitors.length > 9) e.cephMonitors = 'Enter 1 to 9 monitor addresses, one per line.';
      else if (monitors.some((monitor) => !CEPH_MONITOR.test(monitor))) e.cephMonitors = 'Each monitor is an address: host or host:port.';
      const client = form.cephClient.trim();
      if (client.toLowerCase().startsWith('client.')) e.cephClient = 'Enter the Ceph user without its "client." prefix.';
      else if (!CEPH_CLIENT.test(client)) e.cephClient = 'Enter the Ceph user: 1–64 letters, digits, ., _ or -.';
      const cephKeyStored = editing && Boolean(current?.ceph?.key_updated_at);
      const key = form.cephKey.trim();
      if (!key && !cephKeyStored) e.cephKey = "Add the Ceph user's key, as ceph auth get-key prints it.";
      else if (key && !cephKeyLooksRight(key)) e.cephKey = 'This is not a Ceph key: base64 of at least 16 bytes, without spaces.';
    }
    // what the API refuses of a declared vGPU profile, and a flavor declared twice (SDD §4.2, §16, §22.3)
    return declaresGpuProfiles ? { ...e, ...rowProblems(form.gpuProfiles) } : e;
  };

  /** The access to send: what the form declares, without the server-owned key time (SDD §4.2, §28.4). */
  const cephAccess = (): Omit<CephAccess, 'key_updated_at'> | null =>
    form.cephEnabled
      ? { fsid: form.cephFsid.trim().toLowerCase(), monitors: cephMonitorList(form.cephMonitors), client: form.cephClient.trim() || 'seamless' }
      : null;

  const conversionHost = (): ConversionHostConfig | null => {
    const blank = (v: string) => (v.trim() ? v.trim() : null);
    const existing = current?.conversion_host;
    const host: ConversionHostConfig = {
      manage: form.hostManaged,
      name: blank(form.hostName),
      address: form.hostManaged ? null : blank(form.hostAddress),
      flavor: form.hostManaged ? blank(form.hostFlavor) : null,
      image: form.hostManaged ? blank(form.hostImage) : null,
      external_network: form.hostManaged ? blank(form.hostNetwork) : null,
      ssh_user: form.hostUser.trim() || 'cloud-user',
      ssh_allowed_cidr: blank(form.hostCidr),
      ssh_key_secret: !form.hostManaged && form.keySource === 'external' ? form.keySecretName.trim() : (existing?.ssh_key_secret ?? null),
    };
    const empty = host.manage && !host.name && !host.flavor && !host.image && !host.external_network && !host.ssh_allowed_cidr && !existing;
    return empty ? null : host;
  };

  const credentials = (): ProviderCredentials | null => {
    if (!wantsCredentials()) return null;
    const t = (v: string) => v.trim() || undefined;
    if (form.mode === 'vcenter') return { username: t(form.username), password: form.password || undefined, datacenter: t(form.datacenter) };
    if (form.mode === 'application_credential') {
      return { application_credential_id: t(form.appId), application_credential_secret: form.appSecret || undefined, interface: form.iface || undefined };
    }
    return {
      username: t(form.username),
      password: form.password || undefined,
      project_name: t(form.projectName),
      user_domain_name: t(form.userDomain),
      project_domain_name: t(form.projectDomain),
      interface: form.iface || undefined,
    };
  };

  const save = async (test: boolean) => {
    const found = validate();
    setErrors(found);
    setFailure(null);
    if (Object.keys(found).length) {
      if (['hostName', 'hostAddress', 'privateKey', 'keySecretName'].some((k) => k in found)) setHostOpen(true);
      requestAnimationFrame(() => summaryRef.current?.focus());
      return;
    }
    setBusy(true);
    try {
      const fields = {
        name: form.name.trim(),
        endpoint: form.endpoint.trim(),
        region: form.region.trim() || null,
        verify_tls: form.verifyTls,
        ca_cert_path: form.caPath.trim() || null,
        cloud: form.mode === 'clouds_yaml' ? form.cloud.trim() : null,
        // a secret managed outside Seamless is named, never written (SDD §13.3); the other methods write their own
        ...(form.mode === 'external_secret' ? { credentials_secret: form.secretName.trim() } : {}),
        distribution: form.distribution,
        conversion_host: conversionHost(),
        ...(declaresRelease ? { release: form.release || null } : {}),
        ...(declaresProfile ? { policy_profile: form.policyProfile || null } : {}),
        // the map whole (SDD §12, §22.3)
        ...(declaresGpuProfiles ? { gpu_profiles: vgpuProfileMap(form.gpuProfiles) } : {}),
        // a source's Ceph access, never its key time (SDD §28.4); off deletes a stored key
        ...(declaresCeph ? { ceph: cephAccess() as CephAccess | null } : {}),
      };
      // every step that succeeded is kept: a later failure never makes Save add the provider again
      const keep = (step: Provider): Provider => {
        if (!provider) setAdded(step);
        return step;
      };
      let saved: Provider;
      if (current) {
        const patch: ProviderPatch = {};
        for (const [key, value] of Object.entries(fields) as Array<[keyof ProviderPatch, unknown]>) {
          // declared vGPU profiles are one map in any order of their pairs: a PATCH resets the check report (SDD §12, §16)
          const same =
            key === 'gpu_profiles'
              ? sameVgpuProfiles(value as Record<string, string>, current.gpu_profiles ?? {})
              : key === 'ceph'
                ? sameCephAccess(value as CephAccess | null, current.ceph ?? null)
                : JSON.stringify(value) === JSON.stringify(current[key]);
          if (!same) (patch as Record<string, unknown>)[key] = value;
        }
        if (form.mode === 'clouds_yaml' && current.credentials_secret) patch.credentials_secret = null;
        saved = Object.keys(patch).length ? keep(await update.mutateAsync({ id: current.id, patch })) : current;
      } else {
        saved = keep(await create.mutateAsync({ credentials_secret: null, ...fields, id: form.id, kind: preset.kind, role: form.role }));
      }
      const creds = credentials();
      if (creds) saved = keep(await setCredentials.mutateAsync({ id: saved.id, credentials: creds }));
      if (form.keySource === 'upload' && form.privateKey) saved = keep(await setKey.mutateAsync({ id: saved.id, privateKey: form.privateKey }));
      // the Ceph key, written once after the access is saved and never read back (SDD §28.4, plan task CE10)
      if (declaresCeph && form.cephEnabled && form.cephKey.trim()) saved = keep(await setCephKey.mutateAsync({ id: saved.id, key: form.cephKey.trim() }));
      if (test) {
        saved = keep(await check.mutateAsync(saved.id));
        setOutcome({ provider: saved, checked: true });
        requestAnimationFrame(() => outcomeRef.current?.focus());
      } else {
        onSaved?.(saved, `${providerLabel(saved)} ${provider ? 'saved' : 'added'}.`);
        onClose();
      }
    } catch (error) {
      setFailure(error);
    } finally {
      setBusy(false);
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void save(true);
  };

  const onKeyFile = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const text = typeof reader.result === 'string' ? reader.result : '';
      setForm((f) => ({ ...f, privateKey: text, privateKeyLabel: `${file.name} (${file.size} bytes)` }));
    };
    reader.readAsText(file);
    event.target.value = '';
  };

  const errorEntries = Object.entries(errors) as Array<[FieldKey, string]>;
  const credsStored = editing && current?.credentials_updated_at;
  const failureText = failure instanceof ApiError ? failure.message : failure ? String(failure) : null;

  if (outcome) {
    const meta = providerStatusMeta(outcome.provider.status);
    const ok = outcome.provider.status === 'ok';
    return (
      <Modal open={open} onClose={onClose} labelledBy={titleId} size="lg">
        <div className="flex flex-col gap-4 p-5">
          <div className="flex items-start gap-3">
            <PlatformMark preset={preset} size="lg" />
            <div className="min-w-0">
              <h2 ref={outcomeRef} id={titleId} tabIndex={-1} className="text-lg font-semibold text-foreground outline-hidden">
                {ok ? `${providerLabel(outcome.provider)} is connected` : `${providerLabel(outcome.provider)} was saved, but the connection test did not pass`}
              </h2>
              <p className="text-sm text-muted-foreground">Connection test result: {meta.label}.</p>
            </div>
            <ProviderStatusBadge status={outcome.provider.status} size="md" />
          </div>
          {outcome.provider.status_message && (
            <p role="status" className={cn('rounded-md border px-3 py-2 text-sm', ok ? 'border-status-success/40 bg-status-success/10' : 'border-status-warning/50 bg-status-warning/10')}>
              {outcome.provider.status_message}
            </p>
          )}
          {!ok && <p className="text-sm text-muted-foreground">Check the endpoint, the CA and the sign-in details, then test again.</p>}
          <div className="flex flex-wrap justify-end gap-2">
            {!ok && (
              <Button
                size="lg"
                variant="secondary"
                onClick={() => {
                  setOutcome(null);
                  requestAnimationFrame(() => nameRef.current?.focus());
                }}
              >
                Edit again
              </Button>
            )}
            <Button
              size="lg"
              variant="primary"
              onClick={() => {
                onSaved?.(outcome.provider, `${providerLabel(outcome.provider)}: connection ${meta.label.toLowerCase()}.`);
                onClose();
              }}
            >
              Done
            </Button>
          </div>
        </div>
      </Modal>
    );
  }

  return (
    <Modal open={open} onClose={onClose} labelledBy={titleId} size="lg" dismissible={!busy} initialFocusRef={nameRef}>
      <form noValidate onSubmit={submit} className="flex max-h-[85vh] flex-col">
        <div className="border-b border-border px-5 py-4">
          <h2 id={titleId} className="text-lg font-semibold text-foreground">
            {editing ? `Edit ${current ? providerLabel(current) : ''}` : 'Add provider'}
          </h2>
          <p className="text-sm text-muted-foreground">
            {editing
              ? 'Changes apply to new runs; a running or paused plan keeps the provider locked.'
              : 'Register a source to migrate from or a destination to migrate to.'}
          </p>
        </div>

        <div className="relative flex min-h-0 flex-col gap-5 overflow-y-auto px-5 py-4">
          {errorEntries.length > 0 && (
            <div ref={summaryRef} tabIndex={-1} role="alert" className="rounded-md border border-status-danger/60 bg-status-danger/10 px-3 py-2 text-sm outline-none">
              <p className="flex items-center gap-1.5 font-medium text-foreground">
                <CircleAlert aria-hidden className="size-4 text-status-danger" />
                {errorEntries.length === 1 ? 'One field needs attention' : `${errorEntries.length} fields need attention`}
              </p>
              <ul className="mt-1 list-disc pl-6 text-foreground">
                {errorEntries.map(([key, message]) => (
                  <li key={key}>
                    <a
                      href={`#${id(key)}`}
                      className="inline-block py-1 underline underline-offset-2"
                      onClick={(e) => {
                        // to the field itself, as in the plan and policy dialogs; a field of the closed host section opens it first
                        e.preventDefault();
                        const field = document.getElementById(id(key));
                        if (field) {
                          field.focus();
                          return;
                        }
                        setHostOpen(true);
                        requestAnimationFrame(() => document.getElementById(id(key))?.focus());
                      }}
                    >
                      {message}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <Section title="Platform" description="Choosing a platform fills in sensible defaults; you can change every value below.">
            <PlatformPicker value={form.distribution} lockedKind={editing ? (current?.kind ?? null) : null} onChange={choosePreset} />
          </Section>

          <Section title="Connection">
            <div className="grid gap-3 sm:grid-cols-2">
              <TextField
                ref={nameRef}
                id={id('name')}
                label="Name"
                required
                value={form.name}
                onChange={(e) => setName(e.target.value)}
                placeholder={preset.id === 'rhoso' ? 'e.g. RHOSO 18.0 production' : `e.g. ${preset.label} DC1`}
                error={errors.name}
              />
              <TextField
                id={id('id')}
                label="ID"
                required={!editing}
                value={form.id}
                disabled={editing}
                onChange={(e) => setForm((f) => ({ ...f, id: e.target.value, idTouched: true }))}
                hint={editing ? 'The ID is part of every plan that uses this provider; it cannot change.' : 'Used in plans and URLs. Filled in from the name.'}
                error={errors.id}
                inputClassName="font-mono"
                autoComplete="off"
                spellCheck={false}
              />
            </div>
            {preset.roles.length > 1 && !editing ? (
              <div className="flex flex-col gap-1">
                <span className="field-label">Use this cloud as</span>
                <Segmented
                  label="Use this cloud as"
                  value={form.role}
                  onChange={(v) => set('role', v)}
                  options={[
                    { value: 'source', label: 'Migrate from this cloud' },
                    { value: 'destination', label: 'Migrate to this cloud' },
                  ]}
                />
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">
                {form.role === 'source' ? 'Source: VMs are migrated from this cloud.' : 'Destination: VMs are migrated to this cloud.'}
              </p>
            )}
            <TextField
              id={id('endpoint')}
              label={preset.kind === 'vmware' ? 'vCenter URL' : 'Keystone URL'}
              required
              value={form.endpoint}
              onChange={(e) => set('endpoint', e.target.value)}
              placeholder={`e.g. ${preset.endpointExample}`}
              hint={preset.endpointHint}
              error={errors.endpoint}
              inputClassName="font-mono"
              autoComplete="off"
              spellCheck={false}
            />
            <div className="grid gap-3 sm:grid-cols-2">
              <TextField
                id={id('region')}
                label={preset.kind === 'vmware' ? 'Datacenter label' : 'Region'}
                value={form.region}
                onChange={(e) => set('region', e.target.value)}
                placeholder={preset.kind === 'vmware' ? 'e.g. Datacenter-HQ' : 'e.g. regionOne'}
                hint={preset.kind === 'vmware' ? 'Shown on the provider card only.' : 'Leave empty to use the Keystone default.'}
              />
              <TextField
                id={id('ca')}
                label="CA certificate path"
                value={form.caPath}
                onChange={(e) => set('caPath', e.target.value)}
                placeholder="e.g. /etc/pki/seamless/ca.pem"
                hint={preset.caHint}
                inputClassName="font-mono"
                spellCheck={false}
              />
            </div>
            <Toggle
              checked={form.verifyTls}
              onChange={(v) => set('verifyTls', v)}
              label="Verify the TLS certificate"
              hint={form.verifyTls ? 'Recommended. Add the CA path above for a private CA.' : 'Off: anyone between Seamless and this endpoint can read the credentials. Use only in a lab.'}
            />
            {!form.verifyTls && (
              <p className="flex items-start gap-1.5 text-xs text-status-warning">
                <TriangleAlert aria-hidden className="mt-0.5 size-3.5 shrink-0" />
                Certificate verification is off for this provider; the control plane logs a warning on every connection.
              </p>
            )}
            {(declaresRelease || declaresProfile) && (
              <div className="grid gap-3 sm:grid-cols-2">
                {declaresRelease && (
                  <SelectField
                    id={id('release')}
                    label="Release series"
                    value={form.release}
                    onChange={(e) => set('release', e.target.value)}
                    hint="Leave it to the check, which reads the series from the APIs; declare it when they cannot tell it."
                    options={[
                      { value: '', label: 'Detect it from the APIs' },
                      // a series declared outside the window through the API stays selectable
                      ...(form.release && !RELEASE_CHOICES.some((c) => c.value === form.release) ? [{ value: form.release, label: seriesText(form.release) }] : []),
                      ...RELEASE_CHOICES,
                    ]}
                  />
                )}
                <SelectField
                  id={id('policyProfile')}
                  label="Policy profile"
                  value={form.policyProfile}
                  onChange={(e) => set('policyProfile', e.target.value as FormState['policyProfile'])}
                  hint="Which roles Neutron counts for a port on another project's network. Declare Legacy rules only if the cloud turned the secure-RBAC defaults off."
                  options={[
                    { value: '', label: 'By kind and series' },
                    { value: 'secure_rbac', label: 'Secure RBAC defaults' },
                    { value: 'legacy', label: 'Legacy rules' },
                  ]}
                />
              </div>
            )}
          </Section>

          <Section
            title="Sign-in"
            description={
              form.mode === 'external_secret'
                ? 'Read from a secret managed outside Seamless, at each connection. Seamless never writes or shows it.'
                : "Stored in the platform's secret store (an OpenShift Secret, or a 0600 file on Compose). Never kept in the database and never shown again."
            }
          >
            {preset.credentialModes.length > 1 && (
              <Segmented
                label="Sign-in method"
                value={form.mode}
                onChange={(v) => set('mode', v)}
                options={preset.credentialModes.map((m) => ({ value: m, label: MODE_LABELS[m] }))}
              />
            )}
            {form.mode !== 'clouds_yaml' && form.mode !== 'external_secret' && <StoredNote at={credsStored ? current?.credentials_updated_at : null} what="Credentials" />}
            {form.mode === 'clouds_yaml' && (
              <TextField
                id={id('cloud')}
                label="Cloud name"
                required
                value={form.cloud}
                onChange={(e) => set('cloud', e.target.value)}
                placeholder="e.g. rhosp17-dc1"
                hint="The entry under clouds: in the clouds.yaml mounted at SEAMLESS_CLOUDS_YAML. Its password stays in that file."
                error={errors.cloud}
                inputClassName="font-mono"
                spellCheck={false}
              />
            )}
            {form.mode === 'external_secret' && (
              <TextField
                id={id('secretName')}
                label="Secret name"
                required
                value={form.secretName}
                onChange={(e) => set('secretName', e.target.value)}
                placeholder={preset.kind === 'vmware' ? 'e.g. vcenter-dc2' : 'e.g. rhosp17-dc1'}
                hint={`A secret already there for Seamless — files under SEAMLESS_SECRETS_DIR/<name>/, the Secret seamless-<name> on OpenShift, or SEAMLESS_SECRET_<NAME>_* variables; ${preset.kind === 'vmware' ? 'keys username, password and optional datacenter' : 'keys username, password and project_name, or an application credential'}. Seamless reads it and never writes it. ${SECRET_NAME_RULE} ${OPENSHIFT_LABEL}`}
                error={errors.secretName}
                inputClassName="font-mono"
                spellCheck={false}
                autoComplete="off"
              />
            )}
            {(form.mode === 'password' || form.mode === 'vcenter') && (
              <div className="grid gap-3 sm:grid-cols-2">
                <TextField
                  id={id('username')}
                  label={form.mode === 'vcenter' ? 'vCenter user' : 'User name'}
                  value={form.username}
                  onChange={(e) => set('username', e.target.value)}
                  placeholder={form.mode === 'vcenter' ? 'e.g. svc-migrate@vsphere.local' : 'e.g. svc-migrate'}
                  error={errors.username}
                  autoComplete="off"
                  spellCheck={false}
                />
                <TextField
                  id={id('password')}
                  label="Password"
                  type="password"
                  value={form.password}
                  onChange={(e) => set('password', e.target.value)}
                  placeholder={credsStored ? 'Stored. Type to replace it.' : ''}
                  error={errors.password}
                  autoComplete="new-password"
                />
                {form.mode === 'password' ? (
                  <>
                    <TextField
                      id={id('projectName')}
                      label="Project"
                      value={form.projectName}
                      onChange={(e) => set('projectName', e.target.value)}
                      placeholder="e.g. admin"
                      hint="Inventory and quotas are read in this project; an admin role sees every project."
                      error={errors.projectName}
                    />
                    <div className="grid grid-cols-2 gap-3">
                      <TextField id={id('userDomain')} label="User domain" value={form.userDomain} onChange={(e) => set('userDomain', e.target.value)} placeholder="Default" />
                      <TextField id={id('projectDomain')} label="Project domain" value={form.projectDomain} onChange={(e) => set('projectDomain', e.target.value)} placeholder="Default" />
                    </div>
                  </>
                ) : (
                  <TextField
                    id={id('datacenter')}
                    label="Datacenter"
                    value={form.datacenter}
                    onChange={(e) => set('datacenter', e.target.value)}
                    placeholder="e.g. DC2"
                    hint="The vSphere datacenter holding the VMs, when vCenter manages several."
                  />
                )}
              </div>
            )}
            {form.mode === 'application_credential' && (
              <div className="grid gap-3 sm:grid-cols-2">
                <TextField
                  id={id('appId')}
                  label="Application credential ID"
                  value={form.appId}
                  onChange={(e) => set('appId', e.target.value)}
                  error={errors.appId}
                  inputClassName="font-mono"
                  autoComplete="off"
                  spellCheck={false}
                  hint="openstack application credential create seamless --role admin"
                />
                <TextField
                  id={id('appSecret')}
                  label="Secret"
                  type="password"
                  value={form.appSecret}
                  onChange={(e) => set('appSecret', e.target.value)}
                  placeholder={credsStored ? 'Stored. Type to replace it.' : ''}
                  error={errors.appSecret}
                  autoComplete="new-password"
                />
              </div>
            )}
            {(form.mode === 'password' || form.mode === 'application_credential') && (
              <SelectField
                id={id('interface')}
                label="Endpoint interface"
                value={form.iface}
                onChange={(e) => set('iface', e.target.value as FormState['iface'])}
                hint="Which catalog endpoints Seamless calls. Public unless the control plane sits on the internal network."
                options={[
                  { value: '', label: 'Default (public)' },
                  { value: 'public', label: 'Public' },
                  { value: 'internal', label: 'Internal' },
                  { value: 'admin', label: 'Admin' },
                ]}
              />
            )}
          </Section>

          <Section
            title="Conversion host"
            description={
              preset.kind === 'vmware'
                ? 'VMware sources use an existing conversion host with the VDDK; Seamless reaches it over SSH.'
                : 'The helper VM that copies disks. Seamless can deploy it for you.'
            }
          >
            <button
              type="button"
              aria-expanded={hostOpen}
              aria-controls={id('host')}
              onClick={() => setHostOpen((o) => !o)}
              className="flex min-h-11 items-center justify-between gap-2 rounded-md border border-border px-3 text-left text-sm text-foreground hover:bg-muted/60"
            >
              <span className="flex items-center gap-2">
                <PlugZap aria-hidden className="size-4 text-muted-foreground" />
                {form.hostManaged
                  ? form.hostName
                    ? `Deployed by Seamless as ${form.hostName}`
                    : 'Deployed by Seamless'
                  : form.hostAddress || form.hostName
                    ? `Existing host ${form.hostAddress || form.hostName}`
                    : 'Existing host (not configured yet)'}
              </span>
              <span className="text-xs text-muted-foreground">{hostOpen ? 'Hide' : 'Configure'}</span>
            </button>
            {hostOpen && (
              <div id={id('host')} className="flex flex-col gap-3">
                {preset.kind !== 'vmware' && (
                  <Toggle
                    checked={form.hostManaged}
                    onChange={(v) => set('hostManaged', v)}
                    label="Let Seamless deploy the conversion host"
                    hint="Off: use a host you already run, reached over SSH with the key below."
                  />
                )}
                <div className="grid gap-3 sm:grid-cols-2">
                  <TextField id={id('hostName')} label="Host name" value={form.hostName} onChange={(e) => set('hostName', e.target.value)} placeholder="e.g. seamless-conv-src" error={errors.hostName} />
                  {form.hostManaged ? (
                    <TextField id={id('hostFlavor')} label="Flavor" value={form.hostFlavor} onChange={(e) => set('hostFlavor', e.target.value)} placeholder="e.g. m1.large" hint="At least 4 vCPUs and 8 GiB for parallel disks." />
                  ) : (
                    <TextField
                      id={id('hostAddress')}
                      label="Address"
                      value={form.hostAddress}
                      onChange={(e) => set('hostAddress', e.target.value)}
                      placeholder="e.g. 198.51.100.40"
                      error={errors.hostAddress}
                      inputClassName="font-mono"
                    />
                  )}
                  {form.hostManaged && (
                    <>
                      <TextField id={id('hostImage')} label="Image" value={form.hostImage} onChange={(e) => set('hostImage', e.target.value)} placeholder="e.g. rhel-9.4-conversion" />
                      <TextField id={id('hostNetwork')} label="External network" value={form.hostNetwork} onChange={(e) => set('hostNetwork', e.target.value)} placeholder="e.g. provider-ext" />
                    </>
                  )}
                  <TextField id={id('hostUser')} label="SSH user" value={form.hostUser} onChange={(e) => set('hostUser', e.target.value)} />
                  <TextField
                    id={id('hostCidr')}
                    label="SSH allowed from"
                    value={form.hostCidr}
                    onChange={(e) => set('hostCidr', e.target.value)}
                    placeholder="e.g. 10.20.0.0/24"
                    hint="The network of the control plane. Leave empty to allow any address (not recommended)."
                    inputClassName="font-mono"
                  />
                </div>
                {!form.hostManaged && (
                  <Segmented
                    label="SSH key from"
                    value={form.keySource}
                    onChange={(v) => set('keySource', v)}
                    options={[
                      { value: 'upload', label: 'Key stored by Seamless' },
                      { value: 'external', label: 'Key in a secret managed outside Seamless' },
                    ]}
                  />
                )}
                {!form.hostManaged && form.keySource === 'external' && (
                  <TextField
                    id={id('keySecretName')}
                    label="Key secret name"
                    required
                    value={form.keySecretName}
                    onChange={(e) => set('keySecretName', e.target.value)}
                    placeholder="e.g. conversion-dc2-key"
                    hint={`A secret holding the key under private_key (SEAMLESS_SECRETS_DIR/<name>/private_key, the Secret seamless-<name>, or SEAMLESS_SECRET_<NAME>_PRIVATE_KEY). Seamless reads it and never writes it. ${SECRET_NAME_RULE} ${OPENSHIFT_LABEL}`}
                    error={errors.keySecretName}
                    inputClassName="font-mono"
                    spellCheck={false}
                    autoComplete="off"
                  />
                )}
                {!form.hostManaged && form.keySource === 'upload' && (
                  <div className="flex flex-col gap-2" id={id('privateKey')} tabIndex={-1}>
                    <span className="field-label">SSH private key</span>
                    <StoredNote at={current?.conversion_key_updated_at} what="A key" />
                    <div className="flex flex-wrap items-center gap-2">
                      <label className="inline-flex min-h-11 cursor-pointer items-center gap-2 rounded-md border border-border px-3 text-sm text-foreground hover:bg-muted/60 has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-ring">
                        <FileKey2 aria-hidden className="size-4" />
                        Choose key file
                        <input type="file" className="sr-only" onChange={onKeyFile} aria-describedby={errors.privateKey ? `${id('privateKey')}-error` : undefined} />
                      </label>
                      <Button type="button" variant="ghost" size="sm" onClick={() => setPasteKey((p) => !p)}>
                        {pasteKey ? 'Hide paste box' : 'Paste instead'}
                      </Button>
                      {form.privateKeyLabel && (
                        <span className="inline-flex items-center gap-1.5 text-sm text-foreground">
                          <KeyRound aria-hidden className="size-4 text-status-success" />
                          {form.privateKeyLabel} ready to store
                        </span>
                      )}
                    </div>
                    {pasteKey && (
                      <textarea
                        aria-label="SSH private key"
                        className="input min-h-28 font-mono text-xs"
                        spellCheck={false}
                        autoComplete="off"
                        value={form.privateKeyLabel ? '' : form.privateKey}
                        onChange={(e) => setForm((f) => ({ ...f, privateKey: e.target.value, privateKeyLabel: '' }))}
                        placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                      />
                    )}
                    {errors.privateKey && (
                      <p id={`${id('privateKey')}-error`} className="field-error">
                        <CircleAlert aria-hidden className="size-3.5 shrink-0" />
                        {errors.privateKey}
                      </p>
                    )}
                  </div>
                )}
              </div>
            )}
          </Section>

          {declaresCeph && (
            <Section
              title="Ceph access"
              aside={<TechPreviewBadge feature="extents" />}
              description="Lets a plan track changed extents: the warm passes diff Cinder's pass snapshots on the source cluster, so the final pass copies only what changed."
            >
              <Toggle
                checked={form.cephEnabled}
                onChange={(v) => set('cephEnabled', v)}
                label="Declare the source cluster's Ceph access"
                hint="Off: no plan of this source tracks extents, and a stored key is deleted."
              />
              {form.cephEnabled && (
                <div className="grid gap-3 sm:grid-cols-2">
                  <TextField
                    id={id('cephFsid')}
                    label="Cluster fsid"
                    required
                    value={form.cephFsid}
                    onChange={(e) => set('cephFsid', e.target.value)}
                    placeholder="e.g. 6f8c2d4e-1b3a-4c5d-9e7f-0a1b2c3d4e5f"
                    hint="As ceph fsid prints it; a pool on another cluster stays untracked."
                    error={errors.cephFsid}
                    inputClassName="font-mono"
                    spellCheck={false}
                    autoComplete="off"
                  />
                  <TextField
                    id={id('cephClient')}
                    label="Ceph user"
                    required
                    value={form.cephClient}
                    onChange={(e) => set('cephClient', e.target.value)}
                    hint='Without "client.": the metadata-only user of the operations guide.'
                    error={errors.cephClient}
                    inputClassName="font-mono"
                    spellCheck={false}
                    autoComplete="off"
                  />
                  <TextAreaField
                    id={id('cephMonitors')}
                    label="Monitors"
                    required
                    value={form.cephMonitors}
                    onChange={(e) => set('cephMonitors', e.target.value)}
                    hint="One per line, host or host:port, as the source conversion host reaches them (1 to 9)."
                    error={errors.cephMonitors}
                    className="sm:col-span-2"
                    spellCheck={false}
                    autoComplete="off"
                  />
                  <div className="flex flex-col gap-2 sm:col-span-2">
                    <StoredNote at={current?.ceph?.key_updated_at} what="A Ceph key" />
                    <TextField
                      id={id('cephKey')}
                      label="Ceph key"
                      type="password"
                      value={form.cephKey}
                      onChange={(e) => set('cephKey', e.target.value)}
                      hint="As ceph auth get-key prints it. Written once, never shown again."
                      error={errors.cephKey}
                      inputClassName="font-mono"
                      spellCheck={false}
                      autoComplete="off"
                    />
                  </div>
                </div>
              )}
            </Section>
          )}

          {declaresGpuProfiles && (
            <VgpuProfiles
              rows={form.gpuProfiles}
              errors={errors}
              offered={offered}
              hint={gpuHint}
              fieldId={id}
              headingRef={gpuHeadingRef}
              onChange={(rows) => set('gpuProfiles', rows)}
              onRemove={removeGpuProfile}
            />
          )}

          {failureText && <ErrorBanner error={failure} title={editing ? 'Not all changes were saved' : 'The provider could not be saved'} reveal />}
        </div>

        <div className="flex flex-wrap items-center justify-end gap-2 border-t border-border px-5 py-3">
          <Button type="button" size="lg" variant="ghost" onClick={onClose} disabledReason={busy ? 'Wait for the request to finish.' : null}>
            Cancel
          </Button>
          <Button type="button" size="lg" variant="secondary" loading={busy} onClick={() => void save(false)}>
            {editing ? 'Save' : 'Add provider'}
          </Button>
          <Button type="submit" size="lg" variant="primary" icon={PlugZap} loading={busy}>
            {editing ? 'Save and test connection' : 'Add and test connection'}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

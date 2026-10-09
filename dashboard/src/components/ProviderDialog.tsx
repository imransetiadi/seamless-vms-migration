import { CircleAlert, CircleCheck, FileKey2, KeyRound, LockKeyhole, PlugZap, TriangleAlert } from 'lucide-react';
import { useEffect, useId, useRef, useState, type ChangeEvent, type FormEvent, type ReactNode } from 'react';
import { ApiError } from '../api/client';
import { useCheckProvider, useCreateProvider, useSetConversionKey, useSetProviderCredentials, useUpdateProvider } from '../api/hooks';
import type { ConversionHostConfig, Distribution, Provider, ProviderCredentials, ProviderPatch, ProviderRole } from '../api/types';
import { cn } from '../lib/cn';
import { DISTRIBUTION_PRESETS, presetFor, slugify, type CredentialMode, type DistributionPreset } from '../lib/distributions';
import { formatRelative } from '../lib/format';
import { providerStatusMeta } from '../lib/status';
import { Button } from './Button';
import { ErrorBanner } from './ErrorBanner';
import { SelectField, TextField } from './Field';
import { Modal } from './Modal';
import { ProviderStatusBadge } from './StatusBadge';

const ID_PATTERN = /^[a-z0-9][a-z0-9-]{1,62}$/;

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
}

type FieldKey = 'name' | 'id' | 'endpoint' | 'cloud' | 'username' | 'password' | 'projectName' | 'appId' | 'appSecret' | 'hostName' | 'hostAddress' | 'privateKey';
type Errors = Partial<Record<FieldKey, string>>;

function initialState(provider: Provider | undefined): FormState {
  const preset = presetFor(provider?.distribution, provider?.kind ?? 'openstack');
  const host = provider?.conversion_host;
  const mode: CredentialMode =
    preset.kind === 'vmware' ? 'vcenter' : provider?.cloud && !provider.credentials_secret ? 'clouds_yaml' : 'password';
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
  };
}

const MODE_LABELS: Record<CredentialMode, string> = {
  password: 'Username and password',
  application_credential: 'Application credential',
  clouds_yaml: 'clouds.yaml entry',
  vcenter: 'vCenter account',
};

function Section({ title, description, children }: { title: string; description?: ReactNode; children: ReactNode }) {
  const id = useId();
  return (
    <section aria-labelledby={id} className="flex min-w-0 flex-col gap-3 border-t border-border pt-4 first:border-t-0 first:pt-0">
      <div>
        <h3 id={id} className="text-sm font-semibold text-foreground">
          {title}
        </h3>
        {description && <p className="mt-0.5 text-xs text-muted-foreground">{description}</p>}
      </div>
      {children}
    </section>
  );
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
  return (
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
            title={locked ? 'A provider keeps its platform type; register a new provider instead.' : undefined}
          >
            <input
              type="radio"
              name={name}
              value={preset.id}
              checked={checked}
              disabled={locked}
              onChange={() => onChange(preset)}
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
  );
}

function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: Array<{ value: T; label: string }>;
  onChange: (value: T) => void;
}) {
  const name = useId();
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex flex-wrap gap-1 rounded-md border border-border p-1">
      {options.map((option) => (
        <label
          key={option.value}
          className={cn(
            'inline-flex min-h-9 cursor-pointer items-center rounded px-3 text-sm transition-colors duration-150',
            option.value === value ? 'bg-accent text-accent-foreground' : 'text-foreground hover:bg-muted',
            'has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-1 has-[:focus-visible]:outline-ring',
          )}
        >
          <input
            type="radio"
            name={name}
            value={option.value}
            checked={option.value === value}
            onChange={() => onChange(option.value)}
            className="sr-only"
          />
          {option.label}
        </label>
      ))}
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
  const check = useCheckProvider();

  useEffect(() => {
    if (!open) return;
    setForm(initialState(provider));
    setAdded(null);
    setErrors({});
    setOutcome(null);
    setFailure(null);
    setPasteKey(false);
    setHostOpen(Boolean(provider && presetFor(provider.distribution, provider.kind).kind === 'vmware'));
    // Reset only when the dialog opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const preset = presetFor(form.distribution);
  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => setForm((f) => ({ ...f, [key]: value }));
  const id = (key: string) => `provider-dialog-${key}`;

  const choosePreset = (next: DistributionPreset) =>
    setForm((f) => ({
      ...f,
      distribution: next.id,
      role: next.roles.includes(f.role) ? f.role : (next.roles[0] ?? 'source'),
      mode: next.credentialModes.includes(f.mode) ? f.mode : (next.credentialModes[0] ?? 'password'),
      hostManaged: editing ? f.hostManaged : next.managedConversionHost,
    }));

  const setName = (value: string) =>
    setForm((f) => ({ ...f, name: value, id: f.idTouched || editing ? f.id : slugify(value) }));

  const wantsCredentials = () => {
    if (form.mode === 'clouds_yaml') return false;
    if (form.mode === 'vcenter') return Boolean(form.username || form.password || form.datacenter);
    if (form.mode === 'application_credential') return Boolean(form.appId || form.appSecret);
    return Boolean(form.username || form.password || form.projectName);
  };

  const validate = (): Errors => {
    const e: Errors = {};
    if (!form.name.trim()) e.name = 'Enter a name operators will recognise.';
    if (!editing && !ID_PATTERN.test(form.id)) e.id = 'Use 2–63 lowercase letters, digits and dashes, starting with a letter or digit.';
    if (!/^https?:\/\/\S+$/.test(form.endpoint.trim())) e.endpoint = `Enter the full URL, e.g. ${preset.endpointExample}.`;
    const stored = editing && Boolean(current?.credentials_updated_at);
    const required = !editing || !stored || wantsCredentials();
    if (form.mode === 'clouds_yaml') {
      if (!form.cloud.trim()) e.cloud = 'Enter the cloud name of the entry in the mounted clouds.yaml.';
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
      if (!keyStored && !form.privateKey) e.privateKey = 'Add the SSH private key Seamless uses to reach the conversion host.';
    }
    if (form.privateKey && !/^-----BEGIN [A-Z ]*PRIVATE KEY-----/.test(form.privateKey.trim())) {
      e.privateKey = 'This is not an OpenSSH or PEM private key (it must start with -----BEGIN … PRIVATE KEY-----).';
    }
    return e;
  };

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
      ssh_key_secret: existing?.ssh_key_secret ?? null,
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
      if (['hostName', 'hostAddress', 'privateKey'].some((k) => k in found)) setHostOpen(true);
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
        distribution: form.distribution,
        conversion_host: conversionHost(),
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
          if (JSON.stringify(value) !== JSON.stringify(current[key])) (patch as Record<string, unknown>)[key] = value;
        }
        if (form.mode === 'clouds_yaml' && current.credentials_secret) patch.credentials_secret = null;
        saved = Object.keys(patch).length ? keep(await update.mutateAsync({ id: current.id, patch })) : current;
      } else {
        saved = keep(await create.mutateAsync({ ...fields, id: form.id, kind: preset.kind, role: form.role, credentials_secret: null }));
      }
      const creds = credentials();
      if (creds) saved = keep(await setCredentials.mutateAsync({ id: saved.id, credentials: creds }));
      if (form.privateKey) saved = keep(await setKey.mutateAsync({ id: saved.id, privateKey: form.privateKey }));
      if (test) {
        saved = keep(await check.mutateAsync(saved.id));
        setOutcome({ provider: saved, checked: true });
      } else {
        onSaved?.(saved, `${saved.name} ${provider ? 'saved' : 'added'}.`);
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
              <h2 id={titleId} className="text-lg font-semibold text-foreground">
                {ok ? `${outcome.provider.name} is connected` : `${outcome.provider.name} was saved, but the connection test did not pass`}
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
              <Button size="lg" variant="secondary" onClick={() => setOutcome(null)}>
                Edit again
              </Button>
            )}
            <Button
              size="lg"
              variant="primary"
              onClick={() => {
                onSaved?.(outcome.provider, `${outcome.provider.name}: connection ${meta.label.toLowerCase()}.`);
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
            {editing ? `Edit ${current?.name}` : 'Connect a cloud'}
          </h2>
          <p className="text-sm text-muted-foreground">
            {editing
              ? 'Changes apply to new runs; a running or paused plan keeps the provider locked.'
              : 'Register a source to migrate from or the RHOSO destination to migrate to.'}
          </p>
        </div>

        <div className="flex min-h-0 flex-col gap-5 overflow-y-auto px-5 py-4">
          {errorEntries.length > 0 && (
            <div ref={summaryRef} tabIndex={-1} role="alert" className="rounded-md border border-status-danger/60 bg-status-danger/10 px-3 py-2 text-sm outline-none">
              <p className="flex items-center gap-1.5 font-medium text-foreground">
                <CircleAlert aria-hidden className="size-4 text-status-danger" />
                {errorEntries.length === 1 ? 'One field needs attention' : `${errorEntries.length} fields need attention`}
              </p>
              <ul className="mt-1 list-disc pl-6 text-foreground">
                {errorEntries.map(([key, message]) => (
                  <li key={key}>
                    <a href={`#${id(key)}`} className="underline underline-offset-2">
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
          </Section>

          <Section
            title="Sign-in"
            description="Stored in the platform's secret store (an OpenShift Secret, or a 0600 file on Compose). Never kept in the database and never shown again."
          >
            {preset.credentialModes.length > 1 && (
              <Segmented
                label="Sign-in method"
                value={form.mode}
                onChange={(v) => set('mode', v)}
                options={preset.credentialModes.map((m) => ({ value: m, label: MODE_LABELS[m] }))}
              />
            )}
            {form.mode !== 'clouds_yaml' && <StoredNote at={credsStored ? current?.credentials_updated_at : null} what="Credentials" />}
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

          {failureText && <ErrorBanner error={failure} title={editing ? 'Not all changes were saved' : 'The provider could not be saved'} />}
        </div>

        <div className="flex flex-wrap items-center justify-end gap-2 border-t border-border px-5 py-3">
          <Button type="button" size="lg" variant="ghost" onClick={onClose} disabled={busy}>
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

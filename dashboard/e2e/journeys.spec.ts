import { readFileSync } from 'node:fs';
import { expect, test, type Page } from '@playwright/test';

// Operator journeys added with the guest OS, NetApp handover and plan-editing work (SDD §9.5, §7.3.1, §16),
// in the real browser against the mock-mode build. Each test starts from a fresh page, so the in-browser
// mock state (src/api/mock.ts) is its own.
async function signIn(page: Page, token = 'admin') {
  await page.goto('/login');
  await page.getByLabel(/token/i).fill(token);
  await page.getByRole('button', { name: /sign in/i }).click();
  await expect(page).not.toHaveURL(/\/login/);
}

test('an operator edits a validated plan; the change is saved and the plan returns to draft', async ({ page }) => {
  await signIn(page, 'operator');
  await page.goto('/plans/plan-c81d44a0');
  await expect(page.getByRole('heading', { level: 1, name: /shared ceph handover/i })).toBeVisible();
  await expect(page.getByText('Validated', { exact: true }).first()).toBeVisible();

  await page.getByRole('group', { name: /plan actions/i }).getByRole('button', { name: /edit plan/i }).click();
  const dialog = page.getByRole('dialog', { name: 'Edit plan' });
  await expect(dialog.getByLabel(/^name/i)).toHaveValue('Shared Ceph handover — analytics');
  await dialog.getByLabel(/^description/i).fill('Spark nodes move in the Saturday window.');
  await dialog.getByRole('button', { name: 'Save changes' }).click();

  await expect(dialog).toBeHidden();
  await expect(page.getByText('Spark nodes move in the Saturday window.')).toBeVisible();
  // PATCH resets a validated plan to draft: it must be validated again before it starts (SDD §12)
  await expect(page.getByText('Draft', { exact: true }).first()).toBeVisible();
});

test('a keyboard user keeps focus on Validate while its request runs (SDD §16)', async ({ page }) => {
  await signIn(page, 'operator');
  await page.goto('/plans/plan-0e9f6a17');
  const validate = page.getByRole('group', { name: /plan actions/i }).getByRole('button', { name: /^validate$/i });
  await validate.focus();
  await page.keyboard.press('Enter');
  // the mock-mode build answers after 120 ms: the button is busy meanwhile, and a natively disabled
  // button would drop focus to the page
  await expect(page.getByRole('status').filter({ hasText: /validation finished/i })).toBeVisible();
  await expect(validate).not.toHaveAttribute('aria-busy');
  await expect(validate).toBeFocused();
});

test('the shown audit events download as JSON lines, oldest first', async ({ page }) => {
  await signIn(page);
  await page.goto('/events');
  const button = page.getByRole('button', { name: /download shown events/i });
  await expect(button).not.toHaveAttribute('aria-disabled', 'true');

  const [download] = await Promise.all([page.waitForEvent('download'), button.click()]);
  expect(download.suggestedFilename()).toMatch(/^seamless-events-\d{4}-\d{2}-\d{2}T\d{6}\.jsonl$/);
  const lines = readFileSync(await download.path(), 'utf8').trimEnd().split('\n');
  const events = lines.map((line) => JSON.parse(line) as { seq: number; kind: string; ts: string });
  expect(events.length).toBeGreaterThan(1);
  // the format of `seamless events export`: persisted audit events (seq > 0), in sequence order
  const seqs = events.map((event) => event.seq);
  expect(seqs.every((seq) => seq > 0)).toBe(true);
  expect(seqs).toEqual([...seqs].sort((a, b) => a - b));
  expect(events[0].kind).toMatch(/\./);
});

test('the inventory filters by guest OS and Clear filters brings every VM back', async ({ page }) => {
  await signIn(page);
  await page.goto('/inventory/rhosp17-dc1');
  const rows = page.getByRole('table').getByRole('row');
  await expect(rows.nth(1)).toBeVisible();
  const everything = await rows.count();

  await page.getByLabel('Guest OS', { exact: true }).selectOption('windows');
  await expect(rows).toHaveCount(3); // the header and the two Windows Server guests
  await expect(page.getByRole('table')).toContainText('ad-dc-01');
  await expect(page.getByRole('table')).toContainText('report-gen-01');

  await page.getByRole('button', { name: 'Clear filters' }).click();
  await expect(page.getByLabel('Guest OS', { exact: true })).toHaveValue('all');
  await expect(rows).toHaveCount(everything);
});

test('storage handover on NetApp NFS names the RHOSO pool each volume type lands on', async ({ page }) => {
  await signIn(page);
  await page.goto('/plans');
  await page.getByRole('button', { name: /new plan/i }).first().click();
  const dialog = page.getByRole('dialog', { name: /new migration plan/i });
  await dialog.getByLabel(/^name/i).fill('NetApp handover');
  await dialog.getByLabel(/source provider/i).selectOption('rhosp17-dc1');
  await dialog.getByLabel(/destination/i).selectOption('rhoso-prod');
  await dialog.getByRole('checkbox', { name: 'Select report-gen-01' }).click();
  await dialog.getByText(/^advanced/i).click();
  await dialog.getByRole('checkbox', { name: /hand volumes over without copying/i }).click();
  // same export path on another LIF: the source share 192.0.2.60:/cinder_dc1 lands on the RHOSO pool (SDD §7.3.1)
  await expect(dialog.getByText('Lands on hostgroup@ontap-nfs#198.51.100.60:/cinder_dc1')).toBeVisible();

  await dialog.getByRole('button', { name: /create plan with 1 vm/i }).click();
  await expect(page).toHaveURL(/\/plans\/plan-/);
  await expect(page.getByRole('heading', { level: 1, name: 'NetApp handover' })).toBeVisible();
});

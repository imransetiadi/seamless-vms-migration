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
  await dialog.getByLabel(/destination provider/i).selectOption('rhoso-prod');
  await dialog.getByRole('checkbox', { name: 'Select report-gen-01' }).click();
  await dialog.getByText(/^advanced/i).click();
  await dialog.getByRole('checkbox', { name: /hand volumes over without copying/i }).click();
  // same export path on another LIF: the source share 192.0.2.60:/cinder_dc1 lands on the RHOSO pool (SDD §7.3.1)
  await expect(dialog.getByText('Lands on hostgroup@ontap-nfs#198.51.100.60:/cinder_dc1')).toBeVisible();

  await dialog.getByRole('button', { name: /create plan with 1 vm/i }).click();
  await expect(page).toHaveURL(/\/plans\/plan-/);
  await expect(page.getByRole('heading', { level: 1, name: 'NetApp handover' })).toBeVisible();
});

test('an admin approves a decision the Finance policy holds; the audit trail names who approved it (ADV-04)', async ({ page }) => {
  await signIn(page);
  await page.goto('/plans/plan-c81d44a0');
  const panel = page.getByRole('region', { name: 'Waiting for advisor approval' });
  await expect(panel.getByRole('link', { name: 'analytics-node-2', exact: true })).toBeVisible();
  await panel.getByRole('button', { name: 'Approve' }).click();
  await page.getByRole('alertdialog', { name: 'Approve Storage handover for analytics-node-2?' }).getByRole('button', { name: 'Approve' }).click();
  await expect(panel.getByText('Nothing waits for approval any more.')).toBeVisible();
  await expect(panel.getByText('Waiting for advisor approval')).toBeFocused();

  // in-app navigation keeps the in-browser mock state that a page load would reset
  // the Advisor's pages are items of the navigation (SDD §16)
  await page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Audit trail' }).click();
  const approved = page
    .getByRole('row')
    .filter({ hasText: 'Decision: approved' })
    .filter({ hasText: 'user:rina' })
    .filter({ has: page.getByRole('link', { name: 'analytics-node-2', exact: true }) });
  await expect(approved).toHaveCount(1);
});

test('an approver finalizes a completed plan with one approval; it then reads Finalized (ADV-19)', async ({ page }) => {
  await signIn(page, 'approver');
  await page.goto('/plans/plan-2d5c8b93');
  const header = page.locator('header').filter({ has: page.getByRole('heading', { level: 1, name: 'Pilot — static content' }) });
  await expect(header.getByText('Completed', { exact: true })).toBeVisible();
  const queue = page.getByRole('region', { name: 'Awaiting finalize' });
  await expect(queue.getByRole('listitem', { name: 'web-cache-01' })).toContainText('Finalize overdue');

  await page.getByRole('group', { name: /plan actions/i }).getByRole('button', { name: 'Finalize all' }).click();
  const dialog = page.getByRole('alertdialog', { name: 'Finalize 1 VM of Pilot — static content?' });
  await dialog.getByLabel('Type Pilot — static content to confirm').fill('Pilot — static content');
  await dialog.getByRole('button', { name: 'Finalize all' }).click();

  await expect(dialog).toBeHidden();
  await expect(header.getByText('Finalized', { exact: true })).toBeVisible();
  await expect(queue).toBeHidden();
  await expect(page.getByRole('group', { name: 'Finalized' })).toContainText('2');
});

test('powering a finished migration source VM on in demo mode raises the alert on the next watch; off clears it (ADV-19)', async ({ page }) => {
  await signIn(page, 'operator');
  await page.goto('/plans/plan-2d5c8b93');
  await page.getByRole('region', { name: 'Awaiting finalize' }).getByRole('link', { name: 'web-cache-01' }).click();
  const step = page.getByRole('region', { name: 'Next step' });
  await step.getByRole('button', { name: 'Power on the source VM (demo)' }).click();
  await expect(step.getByRole('button', { name: 'Power off the source VM (demo)' })).toBeVisible();
  // the in-browser mock watches every second (the API every SEAMLESS_SOURCE_WATCH_S ÷ demo_speed)
  const alert = page.getByRole('alert', { name: 'Source VM running' });
  await expect(alert).toContainText('its data diverges from RHOSO 18.0 — Production: stop it, or roll back.');

  await step.getByRole('button', { name: 'Power off the source VM (demo)' }).click();
  // migration.source_stopped refreshes the page: no reload
  await expect(alert).toBeHidden();
  await expect(step.getByRole('button', { name: 'Power on the source VM (demo)' })).toBeVisible();
});

test('the Overview inbox lists both kinds of pending approval; the advisor decision opens its recommendation (ADV-20)', async ({ page }) => {
  await signIn(page, 'approver');
  const inbox = page.getByRole('region', { name: 'Pending approvals' });
  await expect(inbox.getByRole('listitem', { name: 'app-billing-01' })).toContainText('Cutover approval');
  const held = inbox.getByRole('listitem', { name: 'analytics-node-2' });
  await expect(held).toContainText('Advisor decision · Migration strategy');
  // one kind at a time; the counts are not fixed: the mock's migrations move on, and another may reach an advisor hold
  // while the test runs (it did on a busy machine)
  await inbox.getByText(/^Cutover approval \(\d+\)$/).click();
  await expect(held).toBeHidden();
  await inbox.getByText(/^Advisor decision \(\d+\)$/).click();
  await expect(inbox.getByRole('listitem', { name: 'app-billing-01' })).toBeHidden();
  await held.getByRole('link', { name: 'analytics-node-2' }).click();
  await expect(page).toHaveURL(/\/advisor\/recommendations\/rec-/);
});

test('a plan names the advisor policy in force beside its cutover approval; the link lands on the policy (ADV-20)', async ({ page }) => {
  await signIn(page, 'viewer');
  await page.goto('/plans/plan-c81d44a0');
  const settings = page.getByRole('region', { name: 'Settings' });
  await expect(settings).toContainText('Asks a person for every migration strategy decision');
  await settings.getByRole('link', { name: 'Finance — approval required' }).click();
  await expect(page).toHaveURL(/\/advisor\/policies#policy-pol-finance$/);
  await expect(page.getByRole('article', { name: 'Finance — approval required' })).toBeFocused();
});

test('a recommendation shows the evidence it rests on; a fact links to its VM in the inventory (ADV-02)', async ({ page }) => {
  await signIn(page, 'viewer');
  await page.goto('/advisor/recommendations/rec-d4e6f8a09001');
  const panel = page.getByRole('region', { name: 'Evidence' });
  await expect(panel).toContainText('Evidence for Warm migration');
  const items = panel.getByRole('list', { name: 'Evidence items' }).getByRole('listitem');
  await expect(items).toHaveCount(6);
  await panel.getByRole('button', { name: /^Show all \(\d+\)$/ }).click();
  const disk = panel.getByRole('listitem', { name: 'Disk size' });
  await expect(disk).toContainText('Context');
  await disk.getByRole('link', { name: 'Inventory' }).click();
  await expect(page).toHaveURL(/\/inventory\/rhosp17-dc1\?vm=/);
  await expect(page.getByLabel('Search VMs')).toHaveValue('static-cdn-01');
  await expect(page.getByRole('table', { name: /VMs on/ }).getByRole('row')).toHaveCount(2);
});

test('on a phone the demo power note sits under its button, not squeezed beside it (ADV-19, SDD §16)', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page, 'operator');
  await page.goto('/plans/plan-2d5c8b93');
  await page.getByRole('region', { name: 'Awaiting finalize' }).getByRole('link', { name: 'web-cache-01' }).click();
  const step = page.getByRole('region', { name: 'Next step' });
  const button = step.getByRole('button', { name: 'Power on the source VM (demo)' });
  const note = step.getByText(/^Demo mode: a source VM running after the cutover/);
  await expect(note).toBeVisible();
  const [b, n] = [await button.boundingBox(), await note.boundingBox()];
  // a flex-1 min-w-0 note shrank to a column of single words (54 px) instead of wrapping below
  expect(n!.width).toBeGreaterThan(200);
  expect(n!.y).toBeGreaterThanOrEqual(b!.y + b!.height);
});

// The control with the focus, unless another element covers its centre (WCAG 2.4.11 Focus Not Obscured).
async function coveredFocus(page: Page): Promise<string | null> {
  return page.evaluate(() => {
    const focused = document.activeElement as HTMLElement | null;
    if (!focused || focused === document.body) return 'BODY';
    const box = focused.getBoundingClientRect();
    const top = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
    if (top && (top === focused || focused.contains(top) || top.contains(focused))) return null;
    return (focused.getAttribute('aria-label') || focused.id || focused.textContent || focused.tagName).trim().slice(0, 60);
  });
}

// How far the dialog's panel scrolls past its content: a body that scrolls on its own leaves it nothing to scroll.
async function panelOverflow(page: Page): Promise<number> {
  return page.evaluate(() => {
    const panel = document.querySelector('[role="dialog"]');
    return panel ? panel.scrollHeight - panel.clientHeight : -1;
  });
}

async function tabCovered(page: Page, stops: number): Promise<string[]> {
  const covered: string[] = [];
  for (let i = 0; i < stops; i += 1) {
    await page.keyboard.press('Tab');
    // WebKit scrolls focused controls asynchronously; inspect after scrolling settles.
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    const hit = await coveredFocus(page);
    if (hit) covered.push(hit);
  }
  return covered;
}

for (const [width, height] of [
  [1280, 800],
  [375, 700],
] as const) {
  test(`at ${width}×${height} the plan, policy and provider dialogs keep the focused control clear of their actions (WCAG 2.4.11, SDD §16)`, async ({ page }) => {
    // up to 120 Tab presses, each checked for a covered focus: seconds alone, but 33.5 s - past the 30 s
    // budget - while other suites shared the machine (audit F-09), a failure for the wrong reason
    test.slow();
    await page.setViewportSize({ width, height });
    await signIn(page, 'admin');
    await page.goto('/plans');
    await page.getByRole('button', { name: 'New plan' }).click();
    const plan = page.getByRole('dialog', { name: 'New migration plan' });
    await plan.getByLabel(/source provider/i).selectOption('rhosp17-dc1');
    await expect(plan.getByRole('table', { name: /^VMs on/ })).toBeVisible();
    await plan.getByLabel(/^name/i).focus();
    expect(await tabCovered(page, 60)).toEqual([]);
    expect(await panelOverflow(page)).toBeLessThanOrEqual(1);
    await page.keyboard.press('Escape');

    await page.goto('/advisor/policies');
    await page.getByRole('button', { name: 'New policy' }).click();
    const policy = page.getByRole('dialog', { name: 'New policy' });
    await policy.getByLabel(/^name/i).focus();
    expect(await tabCovered(page, 30)).toEqual([]);
    expect(await panelOverflow(page)).toBeLessThanOrEqual(1);
    await page.keyboard.press('Escape');

    await page.goto('/providers');
    await page.getByRole('button', { name: /^add provider$/i }).first().click();
    const provider = page.getByRole('dialog', { name: /^add provider$/i });
    await provider.getByLabel(/^name/i).focus();
    expect(await tabCovered(page, 30)).toEqual([]);
    expect(await panelOverflow(page)).toBeLessThanOrEqual(1);
  });
}

test("a community destination shows its series, tier and profile; a declared series its APIs contradict is flagged (SDD §16; plan T8)", async ({ page }) => {
  await signIn(page, 'admin');
  await page.goto('/providers');
  const card = page.getByRole('article', { name: 'OpenStack 2026.2 Hibiscus (DC3)' });
  const row = (term: string) => card.locator('dt', { hasText: term }).locator('xpath=following-sibling::dd[1]');
  await expect(row('Series')).toHaveText(/^2026\.2 Hibiscus\s*declared$/);
  await expect(row('Support tier')).toHaveText('Tier 1');
  await expect(row('Policy profile')).toHaveText('Secure RBAC defaults');
  // the operator declares a series its APIs do not run, then checks: the card says so, and the tier stays the APIs'
  await card.getByRole('button', { name: /^edit /i }).click();
  const dialog = page.getByRole('dialog', { name: /^edit openstack 2026\.2 hibiscus/i });
  await dialog.getByLabel('Release series').selectOption('2024.1');
  await dialog.getByRole('button', { name: 'Save and test connection' }).click();
  await page.getByRole('dialog').getByRole('button', { name: /^done$/i }).click();
  await expect(row('Series')).toContainText('Declared 2024.1 Caracal, which its APIs contradict');
  await expect(row('Support tier')).toHaveText('Tier 1');
});

test('on a phone the lifecycle reads down two columns, half the height of one (SDD §16)', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page, 'viewer');
  await page.goto('/migrations/mig-3c1a0f9e21');
  const steps = page.getByRole('list', { name: 'Migration lifecycle' }).getByRole('listitem');
  await expect(steps.first()).toBeVisible();
  const boxes = await steps.evaluateAll((items) => items.map((el) => el.getBoundingClientRect()).map((r) => ({ x: r.x, y: r.y, bottom: r.bottom })));
  const half = Math.ceil(boxes.length / 2);
  // step 2 is below step 1; the second column starts beside step 1 and runs down as well
  expect(boxes[1].y).toBeGreaterThan(boxes[0].y);
  expect(Math.abs(boxes[half].y - boxes[0].y)).toBeLessThan(2);
  expect(boxes[half].x).toBeGreaterThan(boxes[0].x + 100);
  expect(boxes[half + 1].y).toBeGreaterThan(boxes[half].y);
  // half the rows of the single column it replaces
  expect(Math.max(...boxes.map((b) => b.bottom)) - boxes[0].y).toBeLessThan(half * 40);
  // the desktop row is unchanged: every step on one line
  await page.setViewportSize({ width: 1280, height: 900 });
  const row = await steps.evaluateAll((items) => items.map((el) => Math.round(el.getBoundingClientRect().y)));
  expect(new Set(row).size).toBe(1);
});


test('the navigation is on the left at every width: sidebar, rail, then a drawer from the left (SDD §16)', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await signIn(page);
  const column = page.getByTestId('app-navigation');
  await expect(column).toHaveAttribute('data-nav', 'full');
  const primary = column.getByRole('navigation', { name: 'Primary' });
  await expect(primary.getByRole('link', { name: 'Audit trail' })).toBeVisible();
  expect((await column.boundingBox())?.x).toBe(0);
  expect((await column.boundingBox())?.width).toBe(240);

  await page.setViewportSize({ width: 900, height: 800 });
  await expect(column).toHaveAttribute('data-nav', 'rail');
  await expect(primary.getByRole('link', { name: 'Recommendations' })).toBeVisible();
  expect((await column.boundingBox())?.width).toBe(80);
  // the rail keeps the whole label: it wraps, it is not cut off
  const label = primary.getByRole('link', { name: 'Recommendations' });
  const box = await label.boundingBox();
  expect(await label.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
  expect(box && box.x >= 0 && box.x + box.width <= 80).toBe(true);
  await expect(page.getByRole('button', { name: 'Open navigation' })).toBeHidden();

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(column).toBeHidden();
  await page.getByRole('button', { name: 'Open navigation' }).click();
  const drawer = page.getByRole('dialog', { name: 'Navigation' });
  await expect(drawer).toBeVisible();
  // it slides in from the left edge, then rests against it
  await expect.poll(async () => (await drawer.boundingBox())?.x).toBe(0);
  await expect(drawer.getByRole('link', { name: 'Policies' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(drawer).toBeHidden();
  await expect(page.getByRole('button', { name: 'Open navigation' })).toBeFocused();
});

test("a GPU VM through its mapping, the gate's wait and its cutover", async ({ page }) => {
  // the plan form, a validation, then the gate's three reads a few seconds apart (SDD §22.10): more than a page's time
  test.slow();
  await signIn(page, 'admin');
  await page.goto('/plans');
  await page.getByRole('button', { name: /new plan/i }).first().click();
  const dialog = page.getByRole('dialog', { name: /new migration plan/i });
  await dialog.getByLabel(/^name/i).fill('MIG inference');
  await dialog.getByLabel(/source provider/i).selectOption('vcenter-hq');
  await dialog.getByLabel(/destination provider/i).selectOption('rhoso-prod');
  await dialog.getByRole('checkbox', { name: 'Select dc2-ml-02' }).click();
  // the mapping is the GPU VM's opt-in: nothing is chosen for the operator (SDD §22.4)
  const row = dialog.getByRole('group', { name: 'GPU mappings' }).getByRole('combobox', { name: 'grid_a100-3-20c: 1 VM, 16 vCPUs, 64 GiB' });
  await expect(row).toHaveValue('');
  await row.selectOption('g1.a100-3-20c');
  // a cold copy cuts over from ready, on its own once the gate opens
  await dialog.getByLabel('Default strategy').selectOption('vmware_cold');
  await dialog.getByRole('checkbox', { name: /^Require approval/ }).uncheck();
  await dialog.getByRole('checkbox', { name: /^Automatic cutover/ }).check();
  await dialog.getByText(/^advanced/i).click();
  await dialog.getByLabel(/^network mappings/i).fill('VM Network ERP = erp-net');
  await dialog.getByRole('button', { name: /create plan with 1 vm/i }).click();
  await expect(page).toHaveURL(/\/plans\/plan-/);

  const actions = page.getByRole('group', { name: /plan actions/i });
  await actions.getByRole('button', { name: /^validate$/i }).click();
  await expect(page.getByRole('status').filter({ hasText: /validation finished/i })).toBeVisible();
  await page.getByRole('table', { name: /migrations/i }).getByRole('link', { name: 'dc2-ml-02' }).click();
  // the validation applied the mapping and found room
  const panel = page.getByRole('region', { name: 'GPU' });
  await expect(panel.getByText('g1.a100-3-20c, by the GPU mapping grid_a100-3-20c → g1.a100-3-20c')).toBeVisible();
  await expect(panel.getByText('The validation found room for its GPU flavor.')).toBeVisible();

  // the plan starts: the A100-3-20C pool reads full on the gate's first two reads, the source VM still running
  await page.getByRole('link', { name: 'MIG inference' }).click();
  await actions.getByRole('button', { name: /^start$/i }).click();
  await page.getByRole('alertdialog', { name: 'Start this plan?' }).getByRole('button', { name: 'Start plan' }).click();
  await page.getByRole('table', { name: /migrations/i }).getByRole('link', { name: 'dc2-ml-02' }).click();
  await expect(panel.getByText(/^Waiting for GPU capacity: 0 free of 1 in VGPU CUSTOM_NVIDIA_A100_3_20C, checked .+\. The source VM keeps running\.$/)).toBeVisible();
  // the third read finds room: the wait ends, and the cutover starts
  await expect(panel.getByText(/^Waiting for GPU capacity/)).toBeHidden({ timeout: 20_000 });
  await expect(page.getByRole('list', { name: 'Migration lifecycle' })).toBeVisible();
  await expect(page.locator('header').getByText(/^(Cutover|Verifying|Completed)$/)).toBeVisible();
});

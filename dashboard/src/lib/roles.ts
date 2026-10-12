import { ROLES, type Role } from '../api/types';

/** viewer < operator < approver < admin (SDD §13.2). */
export function roleRank(role: Role | null | undefined): number {
  return role ? ROLES.indexOf(role) : -1;
}

export function hasRole(role: Role | null | undefined, minimum: Role): boolean {
  return roleRank(role) >= roleRank(minimum);
}

/** Named permissions and the minimum role that holds each (SDD §13.2; TODO(ADV-04): confirm admin). */
export const PERMISSIONS = {
  'advisor.policy.manage': 'admin',
  'advisor.approve': 'admin',
} as const satisfies Record<string, Role>;
export type Permission = keyof typeof PERMISSIONS;

export function can(role: Role | null | undefined, permission: Permission): boolean {
  return hasRole(role, PERMISSIONS[permission]);
}

/** Maker-checker (SDD §5.4, §14.6, ADV-17): the API's answers to a requester deciding their own request. */
export const OWN_CUTOVER = 'You requested this cutover. Another approver must approve it.';
/** The API's answer to the approver who forced a cutover approving it (SDD §5.4). */
export const OWN_FORCE = 'You forced this cutover. Another approver must approve it.';
export const OWN_DECISION = 'You requested this decision. Another approver must decide it.';
/** Every request's principal while authentication is off (SDD §13): a reserved token name, so nobody else's. */
export const ANONYMOUS = 'anonymous';
/** What a maker-checker refusal adds while authentication is off: nobody else exists to approve (SDD §13, §16). */
export const AUTH_OFF_HINT = 'With authentication off everyone is anonymous: give each person a token (seamless token create) and turn authentication on.';

/** A maker-checker refusal for `user`, with the way past it while authentication is off; the control stays. */
export function ownReason(reason: string, user: string | null | undefined): string {
  return user === ANONYMOUS ? `${reason} ${AUTH_OFF_HINT}` : reason;
}

export const ROLE_LABELS: Record<Role, string> = {
  viewer: 'Viewer',
  operator: 'Operator',
  approver: 'Approver',
  admin: 'Admin',
};

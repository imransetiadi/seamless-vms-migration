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
export const OWN_DECISION = 'You requested this decision. Another approver must decide it.';

export const ROLE_LABELS: Record<Role, string> = {
  viewer: 'Viewer',
  operator: 'Operator',
  approver: 'Approver',
  admin: 'Admin',
};

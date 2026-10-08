import { ROLES, type Role } from '../api/types';

/** viewer < operator < approver < admin (SDD §13.2). */
export function roleRank(role: Role | null | undefined): number {
  return role ? ROLES.indexOf(role) : -1;
}

export function hasRole(role: Role | null | undefined, minimum: Role): boolean {
  return roleRank(role) >= roleRank(minimum);
}

export const ROLE_LABELS: Record<Role, string> = {
  viewer: 'Viewer',
  operator: 'Operator',
  approver: 'Approver',
  admin: 'Admin',
};

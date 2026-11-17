import { useContext, useEffect } from 'react';
import { UNSAFE_DataRouterContext, useBlocker } from 'react-router-dom';
import { ConfirmDialog } from './ConfirmDialog';

interface Props {
  dirty: boolean;
  pending?: boolean;
  title?: string;
  description?: string;
  confirmLabel?: string;
}

function NavigationGuard({
  dirty,
  pending,
  title = 'Discard unsaved changes?',
  description = 'Your changes have not been saved. Discard them or return to editing.',
  confirmLabel = 'Discard changes',
}: Props) {
  // Section jumps keep the same draft on the page; leaving its route or query can replace it.
  const blocker = useBlocker(({ currentLocation, nextLocation }) => dirty && (
    currentLocation.pathname !== nextLocation.pathname || currentLocation.search !== nextLocation.search
  ));
  return blocker.state === 'blocked' ? (
    <ConfirmDialog
      open
      pending={pending}
      title={title}
      description={description}
      confirmLabel={confirmLabel}
      cancelLabel="Keep editing"
      tone="danger"
      onConfirm={() => blocker.proceed()}
      onCancel={() => blocker.reset()}
    />
  ) : null;
}

/** Drafts and one-time tokens stay in memory; browser unload uses its native warning. */
export function UnsavedChangesGuard(props: Props) {
  const dataRouter = useContext(UNSAFE_DataRouterContext);
  useEffect(() => {
    if (!props.dirty) return;
    const unload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', unload);
    return () => window.removeEventListener('beforeunload', unload);
  }, [props.dirty]);
  return dataRouter ? <NavigationGuard {...props} /> : null;
}

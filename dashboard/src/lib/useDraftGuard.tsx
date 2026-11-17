import { useRef, useState } from 'react';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { UnsavedChangesGuard } from '../components/UnsavedChangesGuard';

export function useDraftGuard(dirty: boolean, pending: boolean, onClose: () => void, { navigation = true } = {}) {
  const [discarding, setDiscarding] = useState(false);
  const focus = useRef<HTMLElement | null>(null);
  const next = useRef<() => void>(onClose);
  const request = (action: () => void) => {
    if (pending) return;
    if (!dirty) {
      action();
      return;
    }
    next.current = action;
    focus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setDiscarding(true);
  };
  const close = () => request(onClose);
  const guard = (
    <>
      {navigation && <UnsavedChangesGuard dirty={dirty || pending} pending={pending} />}
      {discarding && (
        <ConfirmDialog
          open
          title="Discard unsaved changes?"
          description="Your changes have not been saved. Discard them or return to editing."
          confirmLabel="Discard changes"
          cancelLabel="Keep editing"
          tone="danger"
          returnFocusRef={focus}
          onCancel={() => setDiscarding(false)}
          onConfirm={() => {
            setDiscarding(false);
            next.current();
          }}
        />
      )}
    </>
  );
  return { close, request, guard };
}

import { useRef, useState } from 'react';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { UnsavedChangesGuard } from '../components/UnsavedChangesGuard';

export function useDraftGuard(dirty: boolean, pending: boolean, onClose: () => void) {
  const [discarding, setDiscarding] = useState(false);
  const focus = useRef<HTMLElement | null>(null);
  const close = () => {
    if (pending) return;
    if (!dirty) {
      onClose();
      return;
    }
    focus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setDiscarding(true);
  };
  const guard = (
    <>
      <UnsavedChangesGuard dirty={dirty || pending} pending={pending} />
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
            onClose();
          }}
        />
      )}
    </>
  );
  return { close, guard };
}

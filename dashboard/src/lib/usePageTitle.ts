import { useEffect } from 'react';

export const APP_NAME = 'Seamless Migrate';

/** Sets `document.title` to "<title> · Seamless Migrate" while the page is mounted. */
export function usePageTitle(title: string | null | undefined): void {
  useEffect(() => {
    document.title = title ? `${title} · ${APP_NAME}` : APP_NAME;
  }, [title]);
}

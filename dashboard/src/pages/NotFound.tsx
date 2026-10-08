import { MapPinOff } from 'lucide-react';
import { Link } from 'react-router-dom';
import { EmptyState } from '../components/EmptyState';
import { usePageTitle } from '../lib/usePageTitle';

export default function NotFound() {
  usePageTitle('Not found');
  return (
    <>
      <h1 className="sr-only">Page not found</h1>
      <EmptyState
        icon={MapPinOff}
        title="This page does not exist"
        description="The link may be outdated, or the plan or migration was removed."
        action={
          <Link to="/" className="text-sm font-medium text-foreground underline underline-offset-4">
            Go to the overview
          </Link>
        }
      />
    </>
  );
}

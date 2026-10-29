import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, Link, useNavigate, useLocation } from 'react-router-dom';
import { expect, it } from 'vitest';
import { useListState, validListSort } from './useListState';
function List() {
  const state = useListState('test');
  const [query, setQuery] = state.field('query', '');
  const [paused, setPaused] = state.field('paused', false);
  const location = useLocation();
  return <><input aria-label="Search" value={query} onChange={e => setQuery(e.target.value)} /><button onClick={() => setPaused(!paused)}>{paused ? 'Resume' : 'Pause'}</button><Link to="/detail">Detail</Link><output>{location.search}</output></>;
}
function Detail() { const navigate = useNavigate(); return <button onClick={() => navigate(-1)}>Back</button>; }
it('restores query and pause after detail-Back without URL pollution', async () => {
  const user = userEvent.setup();
  render(<MemoryRouter><Routes><Route path="/" element={<List />} /><Route path="/detail" element={<Detail />} /></Routes></MemoryRouter>);
  await user.type(screen.getByLabelText('Search'), 'web-01');
  await user.click(screen.getByText('Pause'));
  await user.click(screen.getByText('Detail'));
  await user.click(screen.getByText('Back'));
  expect(screen.getByLabelText('Search')).toHaveValue('web-01');
  expect(screen.getByText('Resume')).toBeInTheDocument();
  expect(screen.getByRole('status')).toHaveTextContent('');
});

it('validates saved sort keys and direction', () => {
  const valid = validListSort(['name']);
  expect(valid({ key: 'name', direction: 'ascending' })).toBe(true);
  expect(valid({ key: 'other', direction: 'ascending' })).toBe(false);
  expect(valid({ key: 'name', direction: 'sideways' })).toBe(false);
  expect(valid(null)).toBe(true);
});

it('restores distinct entries while the same list remains mounted', async () => {
  function Mounted() {
    const state = useListState('mounted');
    const [query, setQuery] = state.field('query', '');
    const navigate = useNavigate();
    return <><input aria-label="Query" value={query} onChange={e => setQuery(e.target.value)} /><Link to="/?view=second">Next entry</Link><button onClick={() => navigate(-1)}>Back entry</button></>;
  }
  const user = userEvent.setup();
  render(<MemoryRouter><Mounted /></MemoryRouter>);
  await user.type(screen.getByLabelText('Query'), 'first');
  await user.click(screen.getByText('Next entry'));
  expect(screen.getByLabelText('Query')).toHaveValue('');
  await user.type(screen.getByLabelText('Query'), 'second');
  await user.click(screen.getByText('Back entry'));
  expect(screen.getByLabelText('Query')).toHaveValue('first');
});

it('keeps filters on in-place URL replacement without leaking them to another page', async () => {
  function Mounted() {
    const state = useListState('mounted');
    const [query, setQuery] = state.field('query', '');
    const navigate = useNavigate();
    return <><input aria-label="Query" value={query} onChange={e => setQuery(e.target.value)} /><button onClick={() => navigate('/?plan=second', { replace: true })}>Change plan</button><button onClick={() => navigate('/different', { replace: true })}>Change page</button></>;
  }
  const user = userEvent.setup();
  render(<MemoryRouter><Mounted /></MemoryRouter>);
  await user.type(screen.getByLabelText('Query'), 'first');
  await user.click(screen.getByText('Change plan'));
  expect(screen.getByLabelText('Query')).toHaveValue('first');
  await user.click(screen.getByText('Change page'));
  expect(screen.getByLabelText('Query')).toHaveValue('');
});

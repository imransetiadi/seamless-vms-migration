import { render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import { SelectField, TextAreaField, TextField } from './Field';
it('passes required to each native control', () => {
  render(<><TextField label="Name" required /><SelectField label="Choice" required options={[{ value: '', label: 'Choose' }]} /><TextAreaField label="Notes" required /></>);
  for (const label of ['Name', 'Choice', 'Notes']) {
    const control = screen.getByLabelText(new RegExp(`^${label}`)) as HTMLInputElement;
    expect(control.required).toBe(true);
    expect(control.checkValidity()).toBe(false);
  }
});

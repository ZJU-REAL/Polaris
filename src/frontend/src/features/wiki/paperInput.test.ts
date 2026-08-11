import { describe, expect, it } from 'vitest';
import { splitPaperInput } from './paperInput';

describe('splitPaperInput', () => {
  it.each([
    ['comma', '1709.06158,1711.07280'],
    ['space', '1709.06158 1711.07280'],
    ['line feed', '1709.06158\n1711.07280'],
    ['Windows line ending', '1709.06158\r\n1711.07280'],
    ['mixed whitespace and punctuation', '1709.06158\t， 1711.07280；'],
  ])('splits identifiers separated by %s', (_label, input) => {
    expect(splitPaperInput(input)).toEqual(['1709.06158', '1711.07280']);
  });
});

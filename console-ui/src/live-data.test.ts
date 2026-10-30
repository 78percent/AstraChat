import { describe, expect, it } from 'vitest';
import { mergeOlder, type LiveDetail } from './live-data';

const fixture = (id: string, epoch: number, sequences: number[], cursor: number | null): LiveDetail => ({
  session: { id, epoch, channel: 'qq', channelLabel: 'QQ', kind: 'group', peerLabel: '••••1234', personaId: 'fixture', activePersona: true, turnCount: 4, summaryRevision: 1, updatedAt: 1 },
  summary: 'Synthetic summary', summarySourceWatermark: 1, fetchedAt: 1, nextBefore: cursor,
  turns: sequences.map(sequence => ({ sequence, personaRevision: 'fixture', complete: true, createdAt: 1, inputs: [{ text: 'Fixture', displayName: 'Tester' }], replies: [] })),
});
describe('live conversation pagination', () => {
  it('sorts and deduplicates older pages without mutating either source', () => {
    const current = fixture('A', 1, [3, 4], 3), older = fixture('A', 1, [1, 2, 3], null);
    const merged = mergeOlder(current, older);
    expect(merged.turns.map(t => t.sequence)).toEqual([1, 2, 3, 4]);
    expect(merged.nextBefore).toBeNull();
    expect(current.turns).toHaveLength(2);
    expect(older.turns).toHaveLength(3);
  });
  it('refuses records from another session', () => {
    expect(() => mergeOlder(fixture('A', 1, [3], 3), fixture('B', 1, [1], null))).toThrow();
  });
  it('refuses records from an epoch cleared during pagination', () => {
    expect(() => mergeOlder(fixture('A', 1, [3], 3), fixture('A', 2, [1], null))).toThrow();
  });
});

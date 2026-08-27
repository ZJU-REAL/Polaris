export function planRegistrationSequence(items, priorityItemId = null) {
  return [...items].sort((left, right) => {
    if (left.id === priorityItemId && right.id !== priorityItemId) return -1;
    if (right.id === priorityItemId && left.id !== priorityItemId) return 1;
    return left.ordinal - right.ordinal;
  });
}

export function clearStaleSessionPublishError(reset: () => void) {
  reset();
}

export function runFreshSessionPublish(reset: () => void, publish: () => void) {
  clearStaleSessionPublishError(reset);
  publish();
}

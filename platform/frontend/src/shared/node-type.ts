/** Old execution records keep their IDs; their node identity has one current name. */
export function canonicalNodeType(value: string): string {
  return value === "hypothesis" ? "analysis" : value;
}

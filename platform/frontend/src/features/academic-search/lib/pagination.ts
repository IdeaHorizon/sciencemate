/** All pages remain reachable without rendering thousands of page buttons. */
export function paginationItems(page: number, total: number): (number | "gap")[] {
  const selected = new Set([1, total]);
  for (let n=Math.max(1,page-2);n<=Math.min(total,page+2);n++) selected.add(n);
  const items:(number|"gap")[]=[];
  let previous=0;
  for (const n of [...selected].sort((a,b)=>a-b)) {
    if (previous && n>previous+1) items.push("gap");
    items.push(n);previous=n;
  }
  return items;
}

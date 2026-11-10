import {test} from 'node:test';
import assert from 'node:assert/strict';
import {paginationItems} from './pagination.ts';
test('large result sets keep the first, current and last pages reachable',()=>{
  assert.deepEqual(paginationItems(1,1),[1]);
  assert.deepEqual(paginationItems(12,20),[1,'gap',10,11,12,13,14,'gap',20]);
  const pages=paginationItems(10000,12000);
  assert.equal(pages.length,9);
  assert.ok(pages.includes(10000)&&pages.includes(12000));
  assert.deepEqual(paginationItems(12000,12000),[1,'gap',11998,11999,12000]);
});

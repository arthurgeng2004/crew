export const meta = { name: 'crew-wf-selftest', description: '真实 DeepSeek 自测：并行 + schema + 续上下文 / 隔离', phases: [{ title: '并行', detail: '两个全新工人' }, { title: '续上下文', detail: '同一个工人两步' }, { title: '隔离', detail: '全新工人不该知道暗号' }] }
phase('并行')
const nums = await parallel([3, 4].map(k => () => agent(`用 python3 计算 ${k} 的 10 次方，把结果写进 pow${k}.txt（只写数字）。`, {
  label: `${k}^10`, profile: 'ds-fast', schema: { type: 'object', properties: { value: { type: 'integer' } }, required: ['value'] } })))
log(`并行结果：${JSON.stringify(nums)}`)
phase('续上下文')
await agent('记住一个暗号：蓝鲸-47。不要写进任何文件。现在直接调用 submit_result，summary 写“已记住”，files 为空。', { session: 'mem', label: '记暗号', profile: 'ds-fast' })
const recall = await agent('上一步我让你记住的暗号是什么？不要查文件，凭对话回忆，写进 data.code。', {
  session: 'mem', label: '回忆暗号', profile: 'ds-fast', schema: { type: 'object', properties: { code: { type: 'string' } }, required: ['code'] } })
phase('隔离')
const fresh = await agent('在这次对话里，有没有人告诉过你一个暗号？不要查文件。有就把暗号写进 data.code，没有就写“没有”。', {
  label: '新工人', profile: 'ds-fast', schema: { type: 'object', properties: { code: { type: 'string' } }, required: ['code'] } })
return { nums, recall, fresh }

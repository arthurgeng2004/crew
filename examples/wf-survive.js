export const meta = { name: 'crew-survive', description: '后台服务被杀后工作流能否自己恢复（mock）', phases: [{ title: '串行' }] }
const out = []
for (let i = 1; i <= 8; i++) out.push(await agent(`第 ${i} 步`, { label: `s${i}`, profile: 'mock' }))
return out.length

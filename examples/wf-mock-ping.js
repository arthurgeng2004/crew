export const meta = { name: 'crew-mock-ping', description: '检查后台服务能否托管工作流（mock，不调模型）', phases: [{ title: 'ping' }] }
phase('ping')
return await agent('ping', { label: 'ping', profile: 'mock' })

import { defineConfig } from 'vitepress'

export default defineConfig({
  base: '/docs/',
  lang: 'zh-CN',
  title: 'bili2text',
  description: '把 B 站视频转成可修正、可分段的逐字稿，并直接用于打字练习',
  appearance: true,
  lastUpdated: true,
  themeConfig: {
    nav: [
      { text: '快速开始', link: '/project' },
      { text: '开发记录', link: '/log' },
      { text: '开发流程', link: '/sop' },
      {
        text: '展示页',
        link: '/showcase/deck-2026-10-28.html',
        target: '_blank',
      },
    ],
    sidebar: [
      {
        text: '文档中心',
        items: [{ text: '首页', link: '/' }],
      },
      {
        text: '项目',
        items: [
          { text: '项目定义', link: '/project' },
          { text: '开发记录', link: '/log' },
          { text: '提交记录', link: '/commits' },
        ],
      },
      {
        text: '流程与产出',
        items: [
          { text: '开发流程 SOP', link: '/sop' },
          { text: '展示页（幻灯片）', link: '/showcase/deck-2026-10-28.html', target: '_blank' },
          { text: '展示页 v5（卡片式）', link: '/showcase/v5-2026-10-28.html', target: '_blank' },
        ],
      },
    ],
    outline: { level: [2, 3], label: '在此页面' },
    search: { provider: 'local' },
    docFooter: { prev: '上一篇', next: '下一篇' },
    darkModeSwitchLabel: '主题',
    sidebarMenuLabel: '目录',
    returnToTopLabel: '回到顶部',
    lastUpdated: { text: '最后更新' },
    footer: {
      message: '内容来自 PROJECT.md / LOG.md / SOP.md 与 git 历史，由 sync-docs 生成',
      copyright: '单用户本地部署',
    },
  },
})

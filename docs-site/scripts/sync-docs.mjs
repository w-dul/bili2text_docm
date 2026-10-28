import { copyFile, mkdir, readdir, readFile, rm, writeFile } from 'node:fs/promises'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import path from 'node:path'

// docs-site 是附属模块：只从项目根读取文档源与产物，不反向写入
const site = path.resolve(import.meta.dirname, '..')
const root = path.resolve(site, '..')
const pages = path.join(site, 'pages')
const publicDir = path.join(pages, 'public')
const run = promisify(execFile)

const writePage = async (relative, content) => {
  const target = path.join(pages, relative)
  await mkdir(path.dirname(target), { recursive: true })
  await writeFile(target, content, 'utf8')
}

const project = await readFile(path.join(root, 'PROJECT.md'), 'utf8')

// mermaid 源码保留在 PROJECT.md，站内按顺序替换为对应 SVG
const diagrams = [
  { alt: '框架图', file: 'framework.svg' },
  { alt: '主流程图', file: 'flow.svg' },
]
let diagramAt = 0
const projectForDocs = project.replace(/```mermaid[\s\S]*?```/g, () => {
  const diagram = diagrams[diagramAt] ?? diagrams[diagrams.length - 1]
  diagramAt += 1
  return `![${diagram.alt}](../../${diagram.file})`
})
await writePage('project/definition.md', projectForDocs)

for (const [source, relative] of [
  ['LOG.md', 'project/log.md'],
  ['SOP.md', 'project/sop.md'],
]) {
  await writePage(relative, await readFile(path.join(root, source), 'utf8'))
}

// 站点根内的静态资源会由 Vite 处理，自动带上 base
for (const file of ['framework.svg', 'flow.svg']) {
  await copyFile(path.join(root, 'assets', file), path.join(site, file))
}

// 展示页是独立 HTML，只能放在 public 下原样发布
const showcaseTarget = path.join(publicDir, 'showcase')
await rm(showcaseTarget, { recursive: true, force: true })
await mkdir(showcaseTarget, { recursive: true })
for (const name of await readdir(path.join(root, 'showcase'))) {
  await copyFile(path.join(root, 'showcase', name), path.join(showcaseTarget, name))
}

console.log('docs synced: pages/project/{definition,log,sop}.md, framework.svg, public/showcase/')

// 提交记录：git 记事实，LOG.md 记人话，两者互补
const commitsNote = [
  '# 提交记录',
  '',
  '> 由 `git log` 自动生成，每次构建刷新。按版本讲人话的说明见 [开发记录](/project/log)。',
  '',
]
try {
  const { stdout } = await run('git', [
    '-C',
    root,
    'log',
    '-n',
    '80',
    '--date=format:%Y-%m-%d %H:%M',
    '--pretty=format:%h%x09%ad%x09%s',
  ])
  const rows = stdout
    .split('\n')
    .filter(Boolean)
    .map((line) => line.split('\t'))
    .filter((parts) => parts.length === 3)
    .map(([hash, date, subject]) => {
      const [day, time] = date.split(' ')
      return { hash, day, time, subject }
    })

  const byDay = new Map()
  for (const row of rows) {
    if (!byDay.has(row.day)) byDay.set(row.day, [])
    byDay.get(row.day).push(row)
  }

  for (const [day, items] of byDay) {
    commitsNote.push(`## ${day}`, '')
    for (const item of items) {
      commitsNote.push(`- \`${item.hash}\` ${item.time} — ${item.subject}`)
    }
    commitsNote.push('')
  }
  commitsNote.push('> 只展示最近 80 条。分支与合并关系用 Git Graph 类插件查看。', '')
} catch {
  commitsNote.push('> 生成失败：当前环境无法读取 git 历史（未安装 git 或不在仓库内）。', '')
}
await writePage('workspace/commits.md', commitsNote.join('\n'))
console.log('docs synced: pages/workspace/commits.md（最近 80 条提交）')

// 目录对照：网页路径 ↔ 本地文件，省掉"这页在哪个文件里"的来回找
const mapLines = [
  '# 目录对照',
  '',
  '> 网页路径与本地文件的对应关系。改完源文件执行 `npm --prefix docs-site run docs:sync`，页面就会刷新。',
  '',
  '## 页面 ↔ 文件',
  '',
  '| 网页 | 本地文件 | 类型 | 需要同步 |',
  '| --- | --- | --- | --- |',
  '| `/docs/` | `docs-site/pages/index.md` | 手写 | 否 |',
  '| `/docs/project/definition` | `PROJECT.md` → `docs-site/pages/project/definition.md` | 源 → 生成 | 是 |',
  '| `/docs/project/log` | `LOG.md` → `docs-site/pages/project/log.md` | 源 → 生成 | 是 |',
  '| `/docs/project/sop` | `SOP.md` → `docs-site/pages/project/sop.md` | 源 → 生成 | 是 |',
  '| `/docs/workspace/commits` | `git log` → `docs-site/pages/workspace/commits.md` | 自动生成 | 是（自动） |',
  '| `/docs/workspace/map` | `docs-site/scripts/sync-docs.mjs` | 自动生成 | 是（自动） |',
  '| `/docs/showcase/deck-2026-10-28.html` | `showcase/deck-2026-10-28.html` | 源 → 复制 | 是 |',
  '| `/docs/showcase/v5-2026-10-28.html` | `showcase/v5-2026-10-28.html` | 源 → 复制 | 是 |',
  '| 项目定义页内的框架图 | `assets/framework.svg` → `docs-site/framework.svg` | 源 → 复制 | 是 |',
  '| 主流程页内的流程图 | `assets/flow.svg` → `docs-site/flow.svg` | 源 → 复制 | 是 |',
  '| 顶部导航 / 左侧目录 / 页脚 | `docs-site/.vitepress/config.mts` | 手写 | 否（热更新） |',
  '',
  '## 本地目录',
  '',
  '```text',
  'docs-site/',
  '├── pages/                      内容主目录（srcDir）',
  '│   ├── index.md                首页（手写）',
  '│   ├── project/',
  '│   │   ├── definition.md       生成：PROJECT.md',
  '│   │   ├── log.md              生成：LOG.md',
  '│   │   └── sop.md              生成：SOP.md',
  '│   ├── workspace/',
  '│   │   ├── map.md              生成：本页',
  '│   │   └── commits.md          生成：git log',
  '│   └── public/showcase/        生成：展示页',
  '├── framework.svg               生成：assets/framework.svg',
  '├── flow.svg                    生成：assets/flow.svg',
  '├── .vitepress/config.mts       导航与侧栏（手写）',
  '└── scripts/sync-docs.mjs       同步器（手写）',
  '```',
  '',
  '## 常用命令',
  '',
  '```bash',
  'npm --prefix docs-site run docs:sync      # 只同步源文件到站点',
  'npm --prefix docs-site run docs:dev       # 开发服务器，http://localhost:5173/docs/',
  'npm --prefix docs-site run docs:build     # 生成静态产物 dist/',
  'npm --prefix docs-site run docs:preview   # 预览产物，http://localhost:4173/docs/',
  '```',
  '',
]
await writePage('workspace/map.md', mapLines.join('\n'))
console.log('docs synced: pages/workspace/map.md（网页与本地目录对照）')

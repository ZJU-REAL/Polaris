import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vitepress'
import { withMermaid } from 'vitepress-plugin-mermaid'

// The site is served at https://zju-real.github.io/Polaris/
//  - /            → the landing pages (static files in website/public/)
//  - /docs/…      → English docs, sourced from the repo's docs/*.md
//  - /zh/docs/…   → Chinese docs, sourced from docs/zh/*.md
export default withMermaid(
  defineConfig({
    base: '/Polaris/',
    srcDir: '../docs',
    // docs/README.md is the GitHub-facing index; the site uses docs/index.md
    srcExclude: ['README.md'],
    cleanUrls: true,
    lastUpdated: true,
    // docs legitimately point readers at their own local instance
    ignoreDeadLinks: [/^https?:\/\/localhost/],

    vite: {
      resolve: {
        // The markdown sources live outside this project (srcDir ../docs); force
        // bare imports in their compiled components to resolve from our root.
        dedupe: ['vue'],
        alias: {
          vue: fileURLToPath(new URL('../node_modules/vue', import.meta.url)),
          // The markdown sources live outside this project (srcDir ../docs), so the
          // import the mermaid plugin injects into those pages cannot resolve the
          // bare package name from there. Point it back at our node_modules.
          'vitepress-plugin-mermaid/Mermaid.vue': fileURLToPath(
            new URL('../node_modules/vitepress-plugin-mermaid/dist/Mermaid.vue', import.meta.url),
          ),
        },
      },
    },

    title: 'Polaris',
    description:
      'An end-to-end AI research platform: literature, ideas, experiments, paper writing and review.',

    head: [['link', { rel: 'icon', href: '/Polaris/logo.svg' }]],

    // Map repo paths to site URLs: docs/foo.md → /docs/foo, docs/zh/foo.md → /zh/docs/foo
    rewrites: {
      ':page': 'docs/:page',
      'zh/:page': 'zh/docs/:page',
    },

    locales: {
      root: {
        label: 'English',
        lang: 'en',
        themeConfig: {
          nav: [
            { text: 'Home', link: '/index.html', target: '_self' },
            { text: 'Docs', link: '/docs/' },
          ],
          sidebar: {
            '/docs/': [
              {
                text: 'Guide',
                items: [
                  { text: 'Introduction', link: '/docs/' },
                  { text: 'Getting started', link: '/docs/getting-started' },
                  { text: 'Configuration', link: '/docs/configuration' },
                  { text: 'Deployment', link: '/docs/deployment' },
                  { text: 'Development', link: '/docs/development' },
                  { text: 'Desktop app', link: '/docs/desktop' },
                ],
              },
              {
                text: 'Using Polaris',
                items: [
                  { text: 'Literature', link: '/docs/literature' },
                  { text: 'Ideas & idea review', link: '/docs/ideas' },
                  { text: 'Experiments', link: '/docs/experiments' },
                  { text: 'Paper writing', link: '/docs/writing' },
                  { text: 'Paper review', link: '/docs/paper-review' },
                  { text: 'PolarisBuddy', link: '/docs/buddy' },
                  { text: 'Skills', link: '/docs/skills' },
                ],
              },
              {
                text: 'Concepts',
                items: [
                  { text: 'The Voyage agent core', link: '/docs/concepts' },
                  { text: 'Architecture', link: '/docs/architecture' },
                  { text: 'Task system', link: '/docs/task-system' },
                  { text: 'Literature management', link: '/docs/literature-management' },
                  { text: 'Wiki & concepts', link: '/docs/wiki-and-concepts' },
                  { text: 'Embedding & retrieval', link: '/docs/embedding-and-retrieval' },
                ],
              },
              {
                text: 'Integrations',
                items: [{ text: 'MCP', link: '/docs/mcp' }],
              },
            ],
          },
          editLink: {
            pattern: 'https://github.com/ZJU-REAL/Polaris/edit/main/docs/:path',
            text: 'Edit this page on GitHub',
          },
        },
      },
      zh: {
        label: '简体中文',
        lang: 'zh-CN',
        link: '/zh/docs/',
        themeConfig: {
          nav: [
            { text: '官网', link: '/index.html', target: '_self' },
            { text: '文档', link: '/zh/docs/' },
          ],
          sidebar: {
            '/zh/docs/': [
              {
                text: '指南',
                items: [
                  { text: '导览', link: '/zh/docs/' },
                  { text: '快速上手', link: '/zh/docs/getting-started' },
                ],
              },
            ],
          },
          editLink: {
            pattern: 'https://github.com/ZJU-REAL/Polaris/edit/main/docs/:path',
            text: '在 GitHub 上编辑此页',
          },
          outline: { label: '本页目录' },
          docFooter: { prev: '上一页', next: '下一页' },
          lastUpdated: { text: '最近更新' },
          darkModeSwitchLabel: '外观',
          sidebarMenuLabel: '目录',
          returnToTopLabel: '回到顶部',
          langMenuLabel: '切换语言',
        },
      },
    },

    themeConfig: {
      logo: '/logo.svg',
      socialLinks: [{ icon: 'github', link: 'https://github.com/ZJU-REAL/Polaris' }],
      search: {
        provider: 'local',
        options: {
          locales: {
            zh: {
              translations: {
                button: { buttonText: '搜索文档', buttonAriaLabel: '搜索文档' },
                modal: {
                  noResultsText: '没有找到结果',
                  resetButtonTitle: '清除查询',
                  footer: { selectText: '选择', navigateText: '切换', closeText: '关闭' },
                },
              },
            },
          },
        },
      },
      footer: {
        message: 'Released under the Apache-2.0 License.',
        copyright: '© 2026 ZJU-REAL · Polaris',
      },
    },

    mermaid: {},
  }),
)

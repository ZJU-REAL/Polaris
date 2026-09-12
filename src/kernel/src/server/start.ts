/* 容器入口：按**路径**跑，不按包名跑。

   `import('@polaris/kernel/...')` 在镜像里解析不了——pnpm 只把工作区包链进
   依赖它的包的 node_modules，而镜像里没有任何东西依赖 @polaris/kernel，
   所以根目录不会有那个软链。按路径进来则一路相对解析，main.ts 自己的
   依赖（vendor 的 cordis 等）仍从 src/kernel/node_modules 找得到。

   main.ts 只导出 main()、不自调用：那样它才能被测试 import 而不起服务。
   真正「跑起来」的动作放在这里。 */

import { main } from './main.ts'

main().catch((err: unknown) => {
  console.error('[kernel] server host failed to start:', err)
  process.exit(1)
})

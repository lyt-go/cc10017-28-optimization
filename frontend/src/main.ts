import { createApp } from 'vue'
import { createPinia } from 'pinia'

import App from './App.vue'
import router from './router'
import './styles/global.css'

const STORAGE_KEY = 'dispatch-store-v1'

const app = createApp(App)
const pinia = createPinia()

// 持久化分发批次结果：整页刷新或切换页面后仍能看到之前的批次结果与关联记录
pinia.use(({ store }) => {
  if (store.$id !== 'dispatch') return
  store.$subscribe((_mutation, state) => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state))
    } catch {
      // 存储受限时静默降级为内存态，不阻断业务操作
    }
  })
})

app.use(pinia)
app.use(router)
app.mount('#app')

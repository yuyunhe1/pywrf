import { createApp } from 'vue'
import ElButton from 'element-plus/es/components/button/index'
import ElDialog from 'element-plus/es/components/dialog/index'
import ElPopover from 'element-plus/es/components/popover/index'
import ElSelect, { ElOption } from 'element-plus/es/components/select/index'
import ElTable, { ElTableColumn } from 'element-plus/es/components/table/index'
import 'element-plus/dist/index.css'
import App from './App.vue'
import './style.css'
import './workspace.css'

const app = createApp(App)
for (const component of [ElButton, ElDialog, ElOption, ElPopover, ElSelect, ElTable, ElTableColumn]) app.use(component)
app.mount('#app')

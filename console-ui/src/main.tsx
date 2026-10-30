import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Alert, App as AntApp, Button, ConfigProvider, Layout, Menu, Space, Tooltip, Typography, theme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { BellOutlined, ControlOutlined, DatabaseOutlined, DesktopOutlined, IdcardOutlined, LogoutOutlined, MenuFoldOutlined, MenuUnfoldOutlined, MoonOutlined, RobotOutlined, SunOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';
import 'dayjs/locale/zh-cn';
import 'antd/dist/reset.css';
import './styles.css';
import './liquid-glass.css';
import { LiveConsole } from './live';
import { HomePage } from './home';
import { THEME_KEY, readTheme, type Theme } from './preferences';

dayjs.locale('zh-cn');
const { Header, Sider, Content } = Layout;
const { Title } = Typography;
const routeItems = [
  { key: '/home', label: '首页', icon: <DesktopOutlined /> },
  { key: '/context', label: '上下文管理', icon: <DatabaseOutlined /> },
  { key: '/memory', label: '记忆与世界书', icon: <RobotOutlined /> },
  { key: '/personas', label: '角色卡管理', icon: <IdcardOutlined /> },
  { key: '/reminders', label: '定时任务', icon: <BellOutlined /> },
  { key: '/model-settings', label: '个性化配置', icon: <ControlOutlined /> },
];
const navigate = (path: string) => { window.location.hash = path; };
function ConsoleShell({storageError, preference, setPreference}: {storageError: string; preference: Theme; setPreference: React.Dispatch<React.SetStateAction<Theme>>}) {
  const {modal}=AntApp.useApp();
  const [route,setRoute]=useState(window.location.hash.slice(1)||'/home');
  const [collapsed,setCollapsed]=useState(false),[loggedOut,setLoggedOut]=useState(false);
  useEffect(()=>{const onHash=()=>{setRoute(window.location.hash.slice(1)||'/home');window.scrollTo(0,0);};window.addEventListener('hashchange',onHash);return()=>window.removeEventListener('hashchange',onHash);},[]);
  useEffect(()=>{const media=window.matchMedia('(max-width: 700px)');const compact=()=>{if(media.matches)setCollapsed(true);};compact();media.addEventListener('change',compact);return()=>media.removeEventListener('change',compact);},[]);
  const themeLabel=preference==='dark'?'深色':preference==='light'?'浅色':'跟随系统';
  if(loggedOut)return <div className="logout-page"><img src="./kouri-logo.avif" alt="AstraChat logo" /><Title level={3}>已退出控制台</Title><Button type="primary" onClick={()=>setLoggedOut(false)}>重新进入控制台</Button></div>;
  return <Layout className={`app-layout ${collapsed?'is-collapsed':''}`}><a href="#main-content" className="skip-link" onClick={e=>{e.preventDefault();document.getElementById('main-content')?.focus();}}>跳到主要内容</a>
    <Sider width={200} collapsedWidth={72} collapsed={collapsed} className="app-sidebar"><div className="sidebar-inner"><a className="brand" href="#/home" aria-label="AstraChat 控制中心"><img src="./kouri-logo.avif" alt="AstraChat logo" />{!collapsed&&<span><strong>AstraChat</strong><small>控制中心</small></span>}</a><Menu mode="inline" inlineCollapsed={collapsed} selectedKeys={[route]} items={routeItems} onClick={({key})=>navigate(key)} className="main-nav" /><div className="sidebar-footer"><Button type="text" icon={<LogoutOutlined />} aria-label="退出登录" onClick={()=>modal.confirm({title:'退出控制台？',content:'退出后可重新打开查看真实数据。',okText:'退出',cancelText:'取消',onOk:()=>setLoggedOut(true)})}>{!collapsed&&'退出登录'}</Button><Tooltip title={`当前：${themeLabel}，点击切换`}><Button type="text" size="small" aria-label={`切换主题（当前${themeLabel}）`} icon={preference==='system'?<DesktopOutlined />:preference==='dark'?<MoonOutlined />:<SunOutlined />} onClick={()=>setPreference(value=>value==='dark'?'light':value==='light'?'system':'dark')} /></Tooltip></div></div></Sider>
    <Layout className="main-layout"><Header className="topbar"><Space size={16}><Button type="text" aria-label={collapsed?'展开侧栏':'收起侧栏'} icon={collapsed?<MenuUnfoldOutlined />:<MenuFoldOutlined />} onClick={()=>setCollapsed(x=>!x)} /><Title level={4}>控制台</Title></Space><Space><span className="version">v 0.2.0</span></Space></Header><Content id="main-content" tabIndex={-1} className="app-content">{storageError&&<Alert type="error" showIcon message={storageError} className="storage-alert" />}{(route === "/home" || route === "/") ? <HomePage /> : <LiveConsole route={route} />}</Content></Layout>
  </Layout>;
}
function Root() {
  const [preference,setPreference]=useState<Theme>(()=>{try{return readTheme(localStorage);}catch{return 'dark';}});
  const [storageError,setStorageError]=useState('');
  const [systemDark,setSystemDark]=useState(window.matchMedia('(prefers-color-scheme: dark)').matches);
  useEffect(()=>{const media=window.matchMedia('(prefers-color-scheme: dark)');const fn=(e:MediaQueryListEvent)=>setSystemDark(e.matches);media.addEventListener('change',fn);return()=>media.removeEventListener('change',fn);},[]);
  const dark=preference==='system'?systemDark:preference==='dark';
  useEffect(()=>{document.documentElement.dataset.theme=dark?'dark':'light';},[dark]);
  useEffect(()=>{try{localStorage.setItem(THEME_KEY,preference);setStorageError('');}catch{setStorageError('浏览器未能保存主题设置，刷新后可能恢复默认。');}},[preference]);
  return <ConfigProvider locale={zhCN} theme={{algorithm:dark?theme.darkAlgorithm:theme.defaultAlgorithm,token:{colorPrimary:'#32a5b7',colorLink:dark?'#68c5d1':'#267b88',colorLinkHover:dark?'#8adbe5':'#1d6570',colorLinkActive:'#267b88',colorBgLayout:dark?'#07070a':'#f3f3f6',colorText:dark?'#f2f2f5':'#20202a',colorTextSecondary:dark?'#9a9aa6':'#626270',colorBgContainer:dark?'#121218':'#ffffff',colorBgElevated:dark?'#1f1f1f':'#ffffff',borderRadius:14,borderRadiusLG:16,controlHeight:32,fontSize:14},components:{Card:{headerHeight:60,bodyPadding:24,headerPadding:24},Layout:{headerBg:dark?'#000000':'#ffffff',siderBg:dark?'#000000':'#ffffff',headerHeight:64},Menu:{itemBorderRadius:16,itemBg:'transparent',itemSelectedBg:dark?'rgba(255,255,255,.08)':'rgba(0,0,0,.06)',itemSelectedColor:dark?'#e5e7eb':'#141414',itemColor:dark?'rgba(255,255,255,.65)':'rgba(0,0,0,.65)',itemHeight:40},Button:{primaryShadow:'none'},Input:{activeShadow:'0 0 0 2px rgba(50,165,183,.12)'},Modal:{contentBg:dark?'#1f1f1f':'#ffffff'}}}}><AntApp><ConsoleShell storageError={storageError} preference={preference} setPreference={setPreference} /></AntApp></ConfigProvider>;
}
createRoot(document.getElementById('root')!).render(<React.StrictMode><Root /></React.StrictMode>);

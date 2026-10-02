/**
 * La guía de despliegue del agente, para el sistema que el operador ha declarado.
 *
 * ## Por qué esto existe
 *
 * Porque el flujo estaba roto en el sitio más caro que puede estarlo: **funcionaba y no se
 * entendía nada**. El modal entregaba el token y se acababa con un «pégalo en la variable
 * `FENIX_AGENT_TOKEN`» que **no existía** —el agente lee un fichero `.ini` con sección
 * `[plataforma]`—, y con un bloque que decía `url = ` en blanco porque el panel se estaba
 * sirviendo con la variable de la API vacía. Dos fallos en la misma caja, y los dosPorque el
 * flujo está roto en dos sitios a la vez, y ninguno de los dos se ve:
 *
 * 1. **El texto mentía.** Decía «pégalo en `FENIX_AGENT_TOKEN`», que no existe. El agente lee
 *    un fichero `.ini` con sección `[plataforma]`.
 * 2. **El bloque no funcionaba.** Con `VITE_API_URL` vacía —que es como arranca el panel en
 *    desarrollo, porque el navegador usa el proxy de Vite— salía `url = ` en blanco, y un
 *    `.ini` sin URL no arranca: el error es «`url` debe ser una URL https de la plataforma»,
 *    que no dice que el problema es que la caja lo dejó vacío.
 *
 * ## Por qué el sistema se elige al dar de alta y no dentro de la guía
 *
 * Porque la pregunta «¿esto funciona en su Windows Server?» se hace **antes** de desplegar, y
 * el sitio donde se contesta no puede ser un desplegable que hay que abrir. Al registrar el
 * agente se elige el sistema, y la guía —también en la tabla, para cuando ya está dado de
 * alta— enseña las instrucciones de ese sistema.
 *
 * ## Por qué las rutas y el gestor de servicio son **datos**, no texto
 *
 * Porque cambian con el sistema, y un `if` por sistema dentro del JSX es un `if` por sistema
 * que se puede olvidar. Aquí el sistema decide un objeto de rutas y un bloque de servicio, y el
 * JSX es el mismo para los cuatro. Añadir un sistema es añadir una entrada al enum del
 * backend y una a la tabla de aquí, no tocar el cuerpo del componente.
 *
 * ## Por qué Windows usa el Programador de tareas y no `sc.exe`
 *
 * Porque `sc.exe create` registra el programa como servicio, y un proceso de Python no lo es:
 * no implementa el contrato de control de servicio de Windows, así que el servicio arrancaría,
 * moriría al instante y el SCM lo reiniciaría en bucle. La forma que funciona sin dependencias
 * de terceros —Windows Server lo trae de fábrica— es el **Programador de tareas** con
 * `-sc ONSTART`. La alternativa habitual, NSSM o WinSW, exige instalar algo, y la gracia de
 * este agente es que no tiene dependencias: solo la biblioteca estándar.
 */

import { useState } from 'react'
import { AlertTriangle, Check, Copy, FileText, Play, RefreshCw } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { API_BASE_URL } from '../../config'
import { entrecomilla } from '../../lib/shell'
import { SISTEMAS, normalizaSistema } from '../../types/sistema'
import type { SistemaObjetivo } from '../../types/agents'

/**
 * Qué cambia entre un sistema y otro, y nada más.
 *
 * ## Por qué `desconocido` cae en las instrucciones de Linux
 *
 * Porque los comandos de Linux son los que un operador puede leer y corregir aunque el sistema
 * sea otro: le dan la forma del fichero y el orden de los pasos, que es lo que cuesta. Y
 * porque `desconocido` no es un valor de adorno: es lo que hay en las filas antiguas y en las
 * altas que no declaran sistema, y una fila que no dice nada tiene que seguir enseñando algo.
 */
const POR_SISTEMA: Record<
  SistemaObjetivo,
  { rutaConfig: string; tituloServicio: string; cuerpoServicio: string }
> = {
  linux: {
    rutaConfig: '/etc/fenix-agent/fenix-agent.ini',
    tituloServicio: 'fenix-agent.service',
    cuerpoServicio: [
      '[Unit]',
      'Description=__DESCRIPCION__',
      'After=network-online.target',
      '',
      '[Service]',
      'Type=simple',
      'Restart=always',
      'RestartSec=10',
      'ExecStart=__PYTHON__ -m fenix_agent --config __CONFIG__',
      '',
      '[Install]',
      'WantedBy=multi-user.target',
    ].join('\n'),
  },
  windows: {
    rutaConfig: 'C:\\ProgramData\\FenixAgent\\fenix-agent.ini',
    tituloServicio: 'schtasks',
    cuerpoServicio:
      'schtasks /create /tn "FenixAgent" /tr "__PYTHON__ -m fenix_agent --config __CONFIG__" /sc ONSTART /ru SYSTEM /rl HIGHEST',
  },
  macos: {
    rutaConfig: '/usr/local/etc/fenix-agent/fenix-agent.ini',
    tituloServicio: 'com.fenix.agent.plist',
    cuerpoServicio: [
      '<?xml version="1.0" encoding="UTF-8"?>',
      '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"',
      '  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
      '<plist version="1.0">',
      '<dict>',
      '  <key>Label</key><string>com.fenix.agent</string>',
      '  <key>ProgramArguments</key>',
      '  <array>',
      '    <string>__PYTHON__</string>',
      '    <string>-m</string>',
      '    <string>fenix_agent</string>',
      '    <string>--config</string>',
      '    <string>__CONFIG__</string>',
      '  </array>',
      '  <key>RunAtLoad</key><true/>',
      '  <key>KeepAlive</key><true/>',
      '</dict>',
      '</plist>',
    ].join('\n'),
  },
  desconocido: {
    rutaConfig: '/etc/fenix-agent/fenix-agent.ini',
    tituloServicio: 'fenix-agent.service',
    cuerpoServicio: [
      '[Unit]',
      'Description=__DESCRIPCION__',
      'After=network-online.target',
      '',
      '[Service]',
      'Type=simple',
      'Restart=always',
      'RestartSec=10',
      'ExecStart=__PYTHON__ -m fenix_agent --config __CONFIG__',
      '',
      '[Install]',
      'WantedBy=multi-user.target',
    ].join('\n'),
  },
}

/** El intérprete, por sistema. En Windows es `py`, que es el lanzador que viene con Python. */
export type { SistemaObjetivo }

const PYTHON_POR_SISTEMA: Record<SistemaObjetivo, string> = {
  linux: '/usr/bin/python3',
  windows: 'C:\\Program Files\\Python312\\python.exe',
  macos: '/usr/bin/python3',
  desconocido: '/usr/bin/python3',
}

interface Props {
  /**
   * El token del agente recién registrado, o `null` cuando ya no se tiene a mano.
   *
   * ## Por qué admite `null` en lugar de llevar el suyo
   *
   * Porque la plataforma guarda el SHA-256 y no el secreto: la tabla de agentes **no puede**
   * devolverlo, y esta guía se enseña también desde la tabla, cuando un agente lleva tiempo
   * sin conectar. En ese caso sale el bloque con un marcador y se avisa de que el token está
   * perdido, en vez de inventar uno o esconder la guía entera.
   */
  token: string | null
  /** El sistema declarado en el alta. `desconocido` cae en las instrucciones de Linux. */
  sistema: SistemaObjetivo
}

export function AgentSetupGuide({ token, sistema: sistemaDeclarado }: Props) {
  const { t } = useTranslation('agents')
  const [copiado, setCopiado] = useState<string | null>(null)

  /**
   * El sistema que se enseña, y el que se ha pedido.
   *
   * ## Por qué se puede cambiar aquí dentro
   *
   * Porque hay dos casos en los que el sistema de la guía no es el del operador. Uno es una fila
   * antigua, o un alta sin declarar: sale `desconocido` y las instrucciones son las de Linux, que
   * no son las suyas pero tampoco son falsas. El otro es un agente declarado para un sistema y
   * desplegado en otro, que pasa cuando alguien hereda un despliegue.
   *
   * En los dos, lo que falta es poder mirar el otro. Con el selector arriba del todo, se cambia
   * de una pestaña a otra y se ve qué cambia de verdad, sin salir del panel.
   */
  const [sistema, setSistema] = useState<SistemaObjetivo>(normalizaSistema(sistemaDeclarado))
  const rutas = POR_SISTEMA[sistema] ?? POR_SISTEMA.linux
  const python = PYTHON_POR_SISTEMA[sistema] ?? PYTHON_POR_SISTEMA.linux

  /**
   * La URL de la plataforma, o `null` si el panel se está sirviendo sin ella.
   *
   * ## Por qué esto se comprueba en vez de asumir
   *
   * Porque en desarrollo `VITE_API_URL` va vacía a propósito —el navegador usa el proxy de
   * Vite— y `API_BASE_URL` sale como cadena vacía. Con esa cadena, el bloque que se copia
   * contiene `url = ` y el agente muere con un `ConfigError` que habla de un formato de URL
   * inválido, sin mencionar que la caja lo dejó vacío. Un bloque que no se puede copiar bien
   * es peor que un aviso que dice qué hacer.
   */
  const url = API_BASE_URL.trim()
  const urlAusente = url === ''

  const ini = [
    `# ${rutas.rutaConfig}`,
    '[plataforma]',
    `url = ${token === null || !urlAusente ? url : t('setup.urlPlaceholder')}`,
    `token = ${token ?? t('setup.tokenPlaceholder')}`,
    `intervalo_sondeo = ${t('setup.intervalo')}`,
  ].join('\n')

  const comando = `${entrecomilla(python)} -m fenix_agent --config ${entrecomilla(rutas.rutaConfig)}`

  const servicio = rutas.cuerpoServicio
    .replaceAll('__DESCRIPCION__', t('setup.serviceDescription'))
    .replaceAll('__PYTHON__', python)
    .replaceAll('__CONFIG__', entrecomilla(rutas.rutaConfig))

  async function copiar(clave: string, texto: string) {
    try {
      await navigator.clipboard.writeText(texto)
      setCopiado(clave)
    } catch {
      // El portapapeles puede estar bloqueado por permisos. No es motivo para un error en
      // pantalla: el bloque se puede seleccionar y copiar a mano, que es lo que el botón
      // intentaba ahorrar, no lo único posible.
      setCopiado(null)
    }
  }

  return (
    <section className="setup-guide" aria-labelledby="agent-setup-title">
      <div className="section-header">
        <div>
          <h3 id="agent-setup-title">{t('setup.title')}</h3>
          <p className="section-hint">{t('setup.intro')}</p>
        </div>
      </div>

      {/*
        El selector va antes que nada porque es lo primero que hay que decidir: sin él, el que
        tiene un Windows Server lee `/etc/fenix-agent/fenix-agent.ini` y piensa que el panel está
        mal. Es exactamente el punto ciego de la versión anterior, que daba por hecho Linux.
      */}
      <div className="setup-system-tabs" role="group" aria-label={t('setup.systemLabel')}>
        {SISTEMAS.map((opcion) => {
          const activo = opcion === sistema
          return (
            <button
              key={opcion}
              type="button"
              className={activo ? 'setup-system-tab is-active' : 'setup-system-tab'}
              aria-pressed={activo}
              onClick={() => setSistema(opcion)}
            >
              <span className="setup-system-tab-name">{t(`agent.system.${opcion}.name`)}</span>
              <span className="setup-system-tab-hint">{t(`agent.system.${opcion}.hint`)}</span>
            </button>
          )
        })}
      </div>

      {/* Y la diferencia, dicha en una línea, porque es lo que el operador viene a ver. */}
      <p className="setup-differs">{t(`setup.differs.${sistema}`)}</p>

      {urlAusente && (
        <div className="inline-notice inline-notice-warning" role="alert">
          <p className="setup-alert">
            <AlertTriangle size={15} aria-hidden="true" />
            <span>{t('setup.urlMissing')}</span>
          </p>
          <p>{t('setup.urlMissingWhy')}</p>
        </div>
      )}

      {token === null && (
        <p className="inline-notice inline-notice-warning" role="status">
          {t('setup.noToken')}
        </p>
      )}

      <ol className="setup-steps">
        <li>
          <h4>
            <FileText size={15} aria-hidden="true" />
            {t('setup.stepIni')}
          </h4>
          <p className="field-hint">{t('setup.stepIniBody')}</p>
          <Bloque
            titulo={rutas.rutaConfig.split(/[\\/]/).pop() ?? 'fenix-agent.ini'}
            texto={ini}
            clave="ini"
            copiado={copiado}
            onCopiar={copiar}
          />
        </li>

        <li>
          <h4>
            <Play size={15} aria-hidden="true" />
            {t('setup.stepRun')}
          </h4>
          <p className="field-hint">{t('setup.stepRunBody')}</p>
          <Bloque
            titulo={t('setup.runTitle')}
            texto={comando}
            clave="run"
            copiado={copiado}
            onCopiar={copiar}
          />
        </li>

        <li>
          <h4>
            <RefreshCw size={15} aria-hidden="true" />
            {t('setup.stepService')}
          </h4>
          <p className="field-hint">{t(`setup.stepServiceBody.${sistema}`)}
          </p>
          <Bloque
            titulo={rutas.tituloServicio}
            texto={servicio}
            clave="service"
            copiado={copiado}
            onCopiar={copiar}
          />
        </li>
      </ol>

      <div className="setup-notes">
        <div className="setup-note">
          <p className="setup-note-title">{t('setup.verifyTitle')}</p>
          <p>{t('setup.verifyBody')}</p>
        </div>
        <div className="setup-note">
          <p className="setup-note-title">{t('setup.needsTitle')}</p>
          <p>{t('setup.needsBody')}</p>
        </div>
        {sistema === 'windows' && (
          <div className="setup-note setup-note-warning">
            <p className="setup-note-title">{t('setup.permissionsTitle')}</p>
            <p>{t('setup.permissionsBody')}</p>
          </div>
        )}
      </div>
    </section>
  )
}

/** Un bloque de código con su botón de copiar, y el estado de «copiado» por bloque. */
function Bloque({
  titulo,
  texto,
  clave,
  copiado,
  onCopiar,
}: {
  titulo: string
  texto: string
  clave: string
  copiado: string | null
  onCopiar: (clave: string, texto: string) => Promise<void>
}) {
  const { t } = useTranslation('agents')
  const pegado = copiado === clave
  return (
    <div className="code-panel">
      <div className="code-panel-header">
        <span className="code-panel-title">{titulo}</span>
        <button
          type="button"
          className="ghost-button"
          onClick={() => void onCopiar(clave, texto)}
        >
          {pegado ? (
            <Check size={16} aria-hidden="true" />
          ) : (
            <Copy size={16} aria-hidden="true" />
          )}
          <span>{pegado ? t('agent.copied') : t('agent.copy')}</span>
        </button>
      </div>
      <pre className="code-block">
        <code>{texto}</code>
      </pre>
    </div>
  )
}

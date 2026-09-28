import { Cloud, Code2, GitBranch, Globe, Network, Scale, Search } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { CATEGORIAS, TARJETAS_DE_ATAQUE } from './useChat'

/**
 * La pantalla de inicio: que se puede preguntar y cuatro atajos.
 *
 * ## Por que los chips **no** son botones de filtro
 *
 * Porque no filtran nada: rellenan la caja. Un chip que parece un filtro y rellena un campo es
 * la forma mas rapida de que el usuario lo pulse esperando un resultado y no vea ninguno. Se
 * llaman categorias porque es lo que son: un punto de partida. El texto que ponen en
 * la caja es visible, asi que el usuario ve exactamente lo que se va a preguntar antes de
 * enviarlo.
 *
 * Por eso el texto va en la traduccion y no aqui: es lo que el usuario lee, y en ingles tiene
 * que ser ingles.
 *
 * ## Por que las tarjetas rellenan en vez de enviar
 *
 * Porque el prompt de una tarjeta es un **punto de partida**, no una consulta. "Test API
 * authorization" son cuatro palabras: el usuario tiene que decir que API. Enviarlo tal cual
 * gastaria un turno y unos creditos para obtener una respuesta genérica, y el boton que lo
 * hace se llamaria "Enviar" si enviara de verdad.
 */

const ICONOS_DE_CATEGORIA = {
  web: Globe,
  code: Code2,
  cloud: Cloud,
  recon: Search,
  network: Network,
  intel: GitBranch,
  compliance: Scale,
} as const

export interface ChatWelcomeProps {
  onPick: (prompt: string) => void
}

export function ChatWelcome({ onPick }: ChatWelcomeProps) {
  const { t } = useTranslation('chat')

  return (
    <div className="chat-welcome">
      <h2 className="chat-welcome-title">{t('welcome.title')}</h2>
      <p className="chat-welcome-subtitle">{t('welcome.subtitle')}</p>

      <div className="chat-chips" role="group" aria-label={t('welcome.chipsLabel')}>
        {CATEGORIAS.map((categoria) => {
          const Icono = ICONOS_DE_CATEGORIA[categoria]
          return (
            <button
              className="chat-chip"
              type="button"
              key={categoria}
              onClick={() => onPick(t(`categories.${categoria}`))}
            >
              <Icono size={14} aria-hidden="true" />
              {t(`categories.${categoria}`)}
            </button>
          )
        })}
      </div>

      <div className="chat-cards" role="group" aria-label={t('welcome.cardsLabel')}>
        {TARJETAS_DE_ATAQUE.map((tarjeta) => (
          <button
            className="chat-card"
            type="button"
            key={tarjeta}
            onClick={() => onPick(t(`attackCards.${tarjeta}`))}
          >
            <span className="chat-card-title">{t(`attackCards.${tarjeta}.title`)}</span>
            <span className="chat-card-description">{t(`attackCards.${tarjeta}.description`)}</span>
          </button>
        ))}
      </div>
    </div>
  )
}

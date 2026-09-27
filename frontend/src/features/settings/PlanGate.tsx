import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

/**
 * Puerta de plan: lo que se ve en vez de una función de pago.
 *
 * ## Por qué un componente y no una condición en cada vista
 *
 * Hay dos sitios que la necesitan —el candado del submenú y la vista de Audit Logs— y el
 * texto que explica **por qué** es largo: qué incluye, por qué cuesta lo que cuesta, y qué
 * se conserva aunque se cancele. Escribirlo dos veces garantiza que las dos copias digan
 * cosas distintas, y la que se queda corta es la que un cliente lee.
 *
 * ## Por qué el texto va como prop y no dentro del componente
 *
 * Porque la puerta no tiene un mensaje único. La de Audit Logs habla de inmutabilidad y
 * retención; la de otra función de pago hablaría de otra cosa. Un componente que llevara el
 * texto dentro obligaría a que todas las funciones de pago se vendieran con el mismo
 * argumento, y eso es como se acaba vendiendo el mismo argumento a clientes que compran por
 * motivos distintos.
 *
 * ## Por qué el botón lleva a Facturación y no a una página de contacto
 *
 * Porque es lo que la persona quiere hacer. Si la función está bloqueada y hay un camino
 * para desbloquearla dentro del producto, ese es el camino del botón. Mandarla a "contacta
 * con ventas" cuando tiene un botón de suscripción en la pantalla siguiente es un
 * questionnaire sobre si has mirado a la derecha.
 */
interface PlanGateProps {
  title: string
  body: string
  /** Los puntos concretos que incluye. Se pintan como lista, en el orden que se pasan. */
  items: string[]
  actionLabel: string
  actionHref: string
  /**
   * Nota al pie. En Audit Logs es la explicación de por qué un superusuario ve la vista
   * sin plan, que sin explicación parece un fallo de la puerta.
   */
  footnote?: string
  /** Badge del plan, normalmente con candado. */
  badge?: ReactNode
}

export function PlanGate({
  title,
  body,
  items,
  actionLabel,
  actionHref,
  footnote,
  badge,
}: PlanGateProps) {
  return (
    <section className="settings-section">
      <div className="plan-gate">
        {badge !== undefined ? <span className="plan-gate-badge">{badge}</span> : null}
        <h3>{title}</h3>
        <p>{body}</p>
        {items.length > 0 ? (
          <ul>
            {items.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        ) : null}
        <div className="settings-form-actions">
          <Link className="primary-button" to={actionHref}>
            <span>{actionLabel}</span>
          </Link>
        </div>
        {footnote !== undefined ? <p className="field-hint">{footnote}</p> : null}
      </div>
    </section>
  )
}

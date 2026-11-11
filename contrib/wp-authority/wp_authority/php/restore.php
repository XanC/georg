<?php
// SPDX-License-Identifier: AGPL-3.0-only
// Put a backup back: terms (with their original IDs when free), names, slugs, parents,
// descriptions, and every backed-up sermon's assignments; then remove terms the backup
// didn't have, if nothing uses them.
global $wpdb;
$payload = json_decode( base64_decode( '@PAYLOAD@' ), true );
$report  = array(
	'recreated'  => array(),
	'reassigned' => 0,
	'deleted'    => array(),
	'kept'       => array(),
	'errors'     => array(),
);
$map     = array();

foreach ( $payload['terms'] as $tax => $terms ) {
	foreach ( $terms as $t ) {
		$term = get_term( $t['id'], $tax );
		if ( $term && ! is_wp_error( $term ) ) {
			$map[ $tax ][ $t['id'] ] = $t['id'];
			continue;
		}
		$id_free   = ! $wpdb->get_var( $wpdb->prepare( "SELECT term_id FROM $wpdb->terms WHERE term_id = %d", $t['id'] ) );
		$slug_free = ! get_term_by( 'slug', $t['slug'], $tax );
		if ( $id_free && $slug_free ) {
			$wpdb->insert( $wpdb->terms, array( 'term_id' => $t['id'], 'name' => $t['name'], 'slug' => $t['slug'], 'term_group' => 0 ) );
			$wpdb->insert( $wpdb->term_taxonomy, array( 'term_id' => $t['id'], 'taxonomy' => $tax, 'description' => $t['description'], 'parent' => 0, 'count' => 0 ) );
			clean_term_cache( $t['id'], $tax );
			$new = $t['id'];
		} else {
			$made = wp_insert_term( wp_slash( html_entity_decode( $t['name'], ENT_QUOTES ) ), $tax, array( 'slug' => $t['slug'] . '-restored' ) );
			if ( is_wp_error( $made ) ) {
				$report['errors'][] = "$tax {$t['id']}: " . $made->get_error_message();
				continue;
			}
			$new = (int) $made['term_id'];
		}
		$map[ $tax ][ $t['id'] ] = $new;
		$report['recreated'][]   = array( 'taxonomy' => $tax, 'id' => $t['id'], 'now' => $new );
	}
}

foreach ( $payload['sermons'] as $sermon ) {
	if ( ! get_post( $sermon['id'] ) ) {
		continue;
	}
	foreach ( $sermon['terms'] as $tax => $ids ) {
		$want = array();
		foreach ( $ids as $id ) {
			if ( isset( $map[ $tax ][ $id ] ) ) {
				$want[] = $map[ $tax ][ $id ];
			}
		}
		$now = array_map( 'intval', wp_get_object_terms( $sermon['id'], $tax, array( 'fields' => 'ids' ) ) );
		sort( $now );
		$sorted = $want;
		sort( $sorted );
		if ( $now !== $sorted ) {
			wp_set_object_terms( $sermon['id'], $want, $tax );
			++$report['reassigned'];
		}
	}
}

foreach ( $payload['terms'] as $tax => $terms ) {
	$known = array_values( $map[ $tax ] ?? array() );
	foreach ( get_terms( array( 'taxonomy' => $tax, 'hide_empty' => false ) ) as $term ) {
		if ( in_array( (int) $term->term_id, $known, true ) ) {
			continue;
		}
		if ( get_objects_in_term( $term->term_id, $tax ) ) {
			$report['kept'][] = array( 'taxonomy' => $tax, 'id' => (int) $term->term_id, 'name' => $term->name );
		} else {
			wp_delete_term( $term->term_id, $tax );
			$report['deleted'][] = array( 'taxonomy' => $tax, 'id' => (int) $term->term_id, 'name' => $term->name );
		}
	}
	foreach ( $terms as $t ) {
		if ( ! isset( $map[ $tax ][ $t['id'] ] ) ) {
			continue;
		}
		$parent = $t['parent'] ? ( $map[ $tax ][ $t['parent'] ] ?? 0 ) : 0;
		$wpdb->update(
			$wpdb->terms,
			array( 'name' => $t['name'], 'slug' => $t['slug'] ),
			array( 'term_id' => $map[ $tax ][ $t['id'] ] )
		);
		$wpdb->update(
			$wpdb->term_taxonomy,
			array( 'parent' => $parent, 'description' => $t['description'] ),
			array( 'term_id' => $map[ $tax ][ $t['id'] ], 'taxonomy' => $tax )
		);
		clean_term_cache( $map[ $tax ][ $t['id'] ], $tax );
	}
	$all = get_terms( array( 'taxonomy' => $tax, 'hide_empty' => false, 'fields' => 'tt_ids' ) );
	wp_update_term_count_now( $all, $tax );
	delete_option( "{$tax}_children" );
}
echo "\n@@WP-AUTHORITY@@" . wp_json_encode( $report ) . "\n";

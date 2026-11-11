<?php
// SPDX-License-Identifier: AGPL-3.0-only
// Every ctc_sermon's term IDs per taxonomy: { "<post id>": { "<taxonomy>": [ids] } }.
$payload = json_decode( base64_decode( '@PAYLOAD@' ), true );
$ids     = get_posts(
	array(
		'post_type'      => 'ctc_sermon',
		'post_status'    => array( 'publish', 'future', 'draft', 'pending', 'private', 'trash' ),
		'posts_per_page' => -1,
		'fields'         => 'ids',
	)
);
$out     = new stdClass();
foreach ( array_chunk( $ids, 500 ) as $chunk ) {
	foreach ( $payload['taxonomies'] as $taxonomy ) {
		$terms = wp_get_object_terms( $chunk, $taxonomy, array( 'fields' => 'all_with_object_id' ) );
		if ( is_wp_error( $terms ) ) {
			fwrite( STDERR, $terms->get_error_message() . "\n" );
			exit( 1 );
		}
		foreach ( $terms as $term ) {
			$key = (string) $term->object_id;
			if ( ! isset( $out->$key ) ) {
				$out->$key = array();
			}
			$out->$key[ $taxonomy ][] = (int) $term->term_id;
		}
	}
}
echo "\n@@WP-AUTHORITY@@" . wp_json_encode( $out ) . "\n";

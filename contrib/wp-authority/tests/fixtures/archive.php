<?php
// SPDX-License-Identifier: AGPL-3.0-only
$q = new WP_Query( array( 'post_type' => 'ctc_sermon', 'ctc_sermon_speaker' => 'ross-davis', 'posts_per_page' => -1 ) );
echo "\n@@WP-AUTHORITY@@" . wp_json_encode( wp_list_pluck( $q->posts, 'post_title' ) ) . "\n";
